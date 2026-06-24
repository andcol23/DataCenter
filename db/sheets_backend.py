"""
Google Sheets como backend de datos — reemplazo de Supabase/Postgres.

Cada tabla de Supabase es una pestaña (worksheet) del spreadsheet. La fila 1 es la
cabecera (nombres de columna) y el resto son los datos.

Este módulo expone un cliente con una API *fluida* que imita el subconjunto de
`supabase-py` que usa el pipeline, de modo que el resto del código apenas cambia:

    db = get_spreadsheet_client()
    db.table("raw_items").select("*").eq("status", "raw").limit(50).execute().data
    db.table("sources").upsert({...}, on_conflict="name,type").execute()
    db.table("analyzed_items").update({"relevance_score": 0.9}).eq("id", x).execute()
    db.rpc("search_similar_items", {...}).execute()      # → [] (sin embeddings)

Decisiones de diseño:
  • Se EXCLUYEN las columnas `embedding` (analyzed_items) y `body_html` (raw_items),
    y la tabla `fetch_logs` no se migra (logs operativos).
  • Las columnas JSON (listas/objetos) se serializan como texto JSON en la celda.
  • Las fechas se guardan como texto ISO-8601; las comparaciones gte/order son
    lexicográficas (válido para ISO UTC, igual que hacía el código original).
  • La búsqueda semántica (`search_similar_items`) deja de existir → devuelve [].
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# gspread / google-auth se importan de forma perezosa dentro de get_spreadsheet()
# para que importar este módulo no falle si solo se necesitan los esquemas.

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


# ===========================================================================
# Esquemas de cada pestaña (orden de columnas + tipos)
# ===========================================================================

# Por cada tabla definimos:
#   columns   → orden exacto de columnas (cabecera de la pestaña)
#   json_arr  → columnas JSON cuyo default es []  (listas)
#   json_obj  → columnas JSON cuyo default es {}  (objetos)
#   bools     → columnas booleanas
#   ints      → columnas enteras
#   floats    → columnas numéricas decimales
# El resto de columnas se tratan como texto.

SCHEMAS: dict[str, dict[str, Any]] = {
    "sources": {
        "columns": [
            "id", "name", "type", "config", "is_active", "fetch_interval",
            "last_fetched_at", "created_at", "updated_at",
        ],
        "json_obj": {"config"},
        "bools": {"is_active"},
    },
    "raw_items": {
        "columns": [
            "id", "source_id", "external_id", "url", "title", "body_text",
            "author", "published_at", "metadata", "status", "error_message",
            "created_at", "updated_at",
        ],
        "json_obj": {"metadata"},
    },
    # Base de datos curada. Orden de columnas optimizado para lectura humana:
    # las dos fechas primero, luego título original, resumen IA, insights,
    # taxonomía, link original y, por último, ids/metadatos técnicos.
    "analyzed_items": {
        "columns": [
            "created_at", "analyzed_at", "title", "summary", "key_insights",
            "primary_slug", "secondary_slug", "url", "keywords",
            "relevance_score", "novelty_score",
            "id", "raw_item_id", "model_used", "tokens_used", "raw_analysis",
            "updated_at",
        ],
        "json_arr": {"key_insights", "keywords"},
        "json_obj": {"raw_analysis"},
        "ints": {"tokens_used"},
        "floats": {"relevance_score", "novelty_score"},
    },
}

# Tablas que generan id/timestamps automáticamente al insertar.
_AUTO_ID_TABLES = {
    "sources", "raw_items", "analyzed_items",
}

# Relaciones para resolver joins anidados y la vista de candidatos.
#   tabla_local -> (tabla_remota, fk_local, pk_remota)
JOINS: dict[str, tuple[str, str, str]] = {
    "analyzed_items": ("raw_items", "raw_item_id", "id"),
    "raw_items": ("sources", "source_id", "id"),
}


# ===========================================================================
# Serialización / deserialización por columna
# ===========================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _col_kind(schema: dict[str, Any], col: str) -> str:
    if col in schema.get("json_arr", set()):
        return "json_arr"
    if col in schema.get("json_obj", set()):
        return "json_obj"
    if col in schema.get("bools", set()):
        return "bool"
    if col in schema.get("ints", set()):
        return "int"
    if col in schema.get("floats", set()):
        return "float"
    return "text"


# Google Sheets limita cada celda a 50.000 caracteres. Dejamos margen para el
# marcador de truncado y evitamos APIError: [400] al escribir textos largos.
MAX_CELL_CHARS = 50_000
_TRUNC_MARK = "…[truncado]"


def _cap(text: str) -> str:
    """Recorta una cadena al límite de celda de Google Sheets."""
    if len(text) <= MAX_CELL_CHARS:
        return text
    return text[: MAX_CELL_CHARS - len(_TRUNC_MARK)] + _TRUNC_MARK


def serialize_cell(table: str, col: str, value: Any) -> Any:
    """Convierte un valor Python en el valor que se escribe en la celda (RAW)."""
    schema = SCHEMAS[table]
    kind = _col_kind(schema, col)

    if kind in ("json_arr", "json_obj"):
        if value is None or value == "":
            return ""
        return _cap(json.dumps(value, ensure_ascii=False))

    if value is None or value == "":
        return ""

    if kind == "bool":
        return bool(value)
    if kind == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return ""
    if kind == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return ""
    return _cap(str(value))


def deserialize_cell(table: str, col: str, value: Any) -> Any:
    """Convierte el valor crudo leído de la celda en un valor Python tipado."""
    schema = SCHEMAS[table]
    kind = _col_kind(schema, col)

    if kind == "json_arr":
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return []
        return []
    if kind == "json_obj":
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {}
        return {}

    if value == "" or value is None:
        return False if kind == "bool" else None

    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().upper() in ("TRUE", "1", "YES")
    if kind == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    if kind == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return value


def row_to_values(table: str, row: dict[str, Any]) -> list[Any]:
    """Ordena un dict según el esquema y lo serializa a una fila de celdas."""
    cols = SCHEMAS[table]["columns"]
    return [serialize_cell(table, c, row.get(c)) for c in cols]


def values_to_row(table: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Deserializa un registro crudo (header→valor) a un dict Python tipado."""
    cols = SCHEMAS[table]["columns"]
    return {c: deserialize_cell(table, c, raw.get(c, "")) for c in cols}


def _cell_to_value(cell: dict[str, Any]) -> Any:
    """Extrae un valor Python desde CellData de spreadsheets.get."""
    effective = cell.get("effectiveValue") or {}
    if "stringValue" in effective:
        return effective["stringValue"]
    if "numberValue" in effective:
        return effective["numberValue"]
    if "boolValue" in effective:
        return effective["boolValue"]
    if "formulaValue" in effective:
        return effective["formulaValue"]
    return ""


def _value_to_cell(value: Any) -> dict[str, Any]:
    """Convierte un valor Python en CellData para spreadsheets.batchUpdate."""
    if value is None or value == "":
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


# ===========================================================================
# Conexión a Google Sheets
# ===========================================================================

def _fetch_access_token() -> str:
    """Obtiene un access token fresco directamente via HTTP, sin pasar por google-auth."""
    import requests as _req
    import logging

    r = _req.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     os.environ["GOOGLE_CLIENT_ID"].strip(),
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"].strip(),
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(
            f"Error al obtener access token de Google ({r.status_code}): {r.text!r}"
        )
    payload = r.json()
    if "access_token" not in payload:
        raise RuntimeError(
            f"Respuesta de token inválida (falta access_token): {payload}"
        )
    # Log de diagnóstico: muestra los scopes concedidos al access token.
    granted_scope = payload.get("scope", "(scope no devuelto por el endpoint)")
    logging.getLogger(__name__).info("google_token_scopes: %s", granted_scope)
    print(f"[sheets_backend] token_scopes: {granted_scope}", flush=True)
    if granted_scope != "(scope no devuelto por el endpoint)":
        scopes = set(str(granted_scope).split())
        if SHEETS_SCOPE not in scopes:
            raise RuntimeError(
                "El GOOGLE_REFRESH_TOKEN no tiene permiso de Google Sheets. "
                f"Scopes concedidos: {granted_scope!r}. "
                "Regenera el secret con `python tools/google_auth.py` o usa "
                "GOOGLE_SERVICE_ACCOUNT_JSON y comparte el Sheet con esa cuenta."
            )
    return payload["access_token"]


def get_spreadsheet():
    """Abre el spreadsheet usando un Bearer token directo — sin AuthorizedSession."""
    import json as _json
    import requests as _req
    import gspread
    from gspread.http_client import HTTPClient

    class _SheetsHTTPClient(HTTPClient):
        def request(
            self,
            method,
            endpoint,
            params=None,
            data=None,
            json=None,
            files=None,
            headers=None,
        ):
            response = self.session.request(
                method=method,
                url=endpoint,
                json=json,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout,
            )
            if response.ok:
                return response

            content_type = response.headers.get("content-type", "")
            body = response.text[:1000] if response.text else "(empty response body)"
            raise RuntimeError(
                f"Sheets API error {response.status_code} [{method.upper()} {endpoint}] "
                f"content-type={content_type!r}: {body!r}"
            )

    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError(
            "GOOGLE_SHEET_ID no está definido en .env. "
            "Es el ID del spreadsheet (la parte de la URL entre /d/ y /edit)."
        )

    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if service_account_json:
        try:
            service_account_info = _json.loads(service_account_json)
        except _json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON no es JSON válido. "
                "Guarda el contenido completo del key JSON como secret."
            ) from exc
        return gspread.service_account_from_dict(
            service_account_info,
            http_client=_SheetsHTTPClient,
        ).open_by_key(sheet_id)

    access_token = _fetch_access_token()

    # Sesión plain-requests con el Bearer hardcodeado: google-auth no interviene
    # en ninguna petición, eliminando el riesgo de reauth que trunca los scopes.
    _session = _req.Session()
    _session.headers["Authorization"] = f"Bearer {access_token}"

    class _BearerHTTPClient(_SheetsHTTPClient):
        def __init__(self, auth, session=None):
            self.auth = None
            self.timeout = 120
            self.session = _session  # ignoramos auth/session: usamos el Bearer directo

    gc = gspread.Client(auth=None, http_client=_BearerHTTPClient)
    return gc.open_by_key(sheet_id)


# ===========================================================================
# Store: lectura/escritura con caché en memoria por pestaña
# ===========================================================================

class _Store:
    """Mantiene una copia en memoria de cada pestaña y la sincroniza con Sheets."""

    def __init__(self, spreadsheet):
        self.ss = spreadsheet
        self._ws: dict[str, Any] = {}
        self._records: dict[str, list[dict[str, Any]]] = {}
        self._rownums: dict[str, list[int]] = {}

    def worksheet(self, table: str):
        if table not in self._ws:
            self._ws[table] = self.ss.worksheet(table)
        return self._ws[table]

    def _load(self, table: str) -> None:
        ws = self.worksheet(table)
        metadata = self.ss.fetch_sheet_metadata(
            params={"includeGridData": "true", "ranges": [f"'{table}'!A:Z"]}
        )
        sheet = next(
            (
                s
                for s in metadata.get("sheets", [])
                if s.get("properties", {}).get("sheetId") == ws.id
            ),
            None,
        )
        row_data = ((sheet or {}).get("data") or [{}])[0].get("rowData") or []
        values = [
            [_cell_to_value(cell) for cell in row.get("values", [])]
            for row in row_data
        ]
        header = values[0] if values else []
        recs: list[dict[str, Any]] = []
        rownums: list[int] = []
        for i, row in enumerate(values[1:]):
            raw = {
                col: row[idx] if idx < len(row) else ""
                for idx, col in enumerate(header)
            }
            recs.append(values_to_row(table, raw))
            rownums.append(i + 2)  # fila 1 = cabecera
        self._records[table] = recs
        self._rownums[table] = rownums

    def records(self, table: str) -> list[dict[str, Any]]:
        if table not in self._records:
            self._load(table)
        return self._records[table]

    def append(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        recs = self.records(table)
        ws = self.worksheet(table)
        rownum = len(recs) + 2
        self.ss.batch_update(
            {
                "requests": [
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": ws.id,
                                "rowIndex": rownum - 1,
                                "columnIndex": 0,
                            },
                            "rows": [
                                {
                                    "values": [
                                        _value_to_cell(value)
                                        for value in row_to_values(table, row)
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue",
                        }
                    }
                ],
            }
        )
        recs.append(dict(row))
        self._rownums[table].append(rownum)
        return dict(row)

    def update_at(self, table: str, idx: int, row: dict[str, Any]) -> dict[str, Any]:
        recs = self.records(table)
        ws = self.worksheet(table)
        rownum = self._rownums[table][idx]
        self.ss.batch_update(
            {
                "requests": [
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": ws.id,
                                "rowIndex": rownum - 1,
                                "columnIndex": 0,
                            },
                            "rows": [
                                {
                                    "values": [
                                        _value_to_cell(value)
                                        for value in row_to_values(table, row)
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue",
                        }
                    }
                ],
            }
        )
        recs[idx] = dict(row)
        return dict(row)


# ===========================================================================
# Resultado y emulador de queries
# ===========================================================================

class _Result:
    def __init__(self, data: Any):
        self.data = data


def _matches(row: dict[str, Any], col: str, op: str, val: Any) -> bool:
    cur = row.get(col)
    if op == "eq":
        if isinstance(cur, bool) or isinstance(val, bool):
            return bool(cur) == bool(val)
        return str(cur) == str(val) if cur is not None else val is None
    if op == "neq":
        return str(cur) != str(val)
    if op == "gte":
        return cur is not None and str(cur) >= str(val)
    if op == "lte":
        return cur is not None and str(cur) <= str(val)
    if op == "in":
        return cur in val
    if op == "is_null":
        return cur is None
    if op == "is_not_null":
        return cur is not None
    if op == "ilike":
        pat = str(val).strip("%").lower()
        return pat in str(cur or "").lower()
    return False


class _Query:
    """Emula el query-builder de supabase-py sobre el _Store."""

    def __init__(self, store: _Store, table: str):
        self.store = store
        self.table = table
        self._select = "*"
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._single = False
        self._negate_next = False
        # operación de escritura pendiente
        self._op: str | None = None
        self._payload: Any = None
        self._on_conflict: str | None = None
        self._ignore_duplicates = False

    # ── lectura ───────────────────────────────────────────────────────────
    def select(self, columns: str = "*", *_a, **_k) -> "_Query":
        self._select = columns
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, "eq", val))
        return self

    def neq(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, "neq", val))
        return self

    def gte(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, "gte", val))
        return self

    def lte(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, "lte", val))
        return self

    def in_(self, col: str, vals: Iterable[Any]) -> "_Query":
        self._filters.append((col, "in", list(vals)))
        return self

    def ilike(self, col: str, pattern: str) -> "_Query":
        self._filters.append((col, "ilike", pattern))
        return self

    @property
    def not_(self) -> "_Query":
        self._negate_next = True
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        # `is_(col, "null")` → IS NULL; con `.not_` delante → IS NOT NULL
        is_null = (val is None) or (str(val).lower() == "null")
        if is_null:
            op = "is_not_null" if self._negate_next else "is_null"
        else:
            op = "neq" if self._negate_next else "eq"
        self._negate_next = False
        self._filters.append((col, op, val))
        return self

    def order(self, col: str, desc: bool = False, *_a, **_k) -> "_Query":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def single(self) -> "_Query":
        self._single = True
        return self

    def maybe_single(self) -> "_Query":
        self._single = True
        return self

    # ── escritura ─────────────────────────────────────────────────────────
    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def upsert(
        self,
        payload: dict[str, Any] | list[dict[str, Any]],
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
        *_a,
        **_k,
    ) -> "_Query":
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    # ── ejecución ─────────────────────────────────────────────────────────
    def execute(self) -> _Result:
        if self._op == "insert":
            return self._do_insert()
        if self._op == "update":
            return self._do_update()
        if self._op == "upsert":
            return self._do_upsert()
        return self._do_select()

    # ── helpers de escritura ──────────────────────────────────────────────
    def _prepare_new(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload)
        if self.table in _AUTO_ID_TABLES and not row.get("id"):
            row["id"] = str(uuid.uuid4())
        now = _now_iso()
        cols = SCHEMAS[self.table]["columns"]
        if "created_at" in cols and not row.get("created_at"):
            row["created_at"] = now
        if "updated_at" in cols and not row.get("updated_at"):
            row["updated_at"] = now
        # rellenar columnas ausentes con None para mantener el ancho de la fila
        return {c: row.get(c) for c in cols}

    def _do_insert(self) -> _Result:
        payloads = self._payload if isinstance(self._payload, list) else [self._payload]
        out = []
        for p in payloads:
            row = self._prepare_new(p)
            self.store.append(self.table, row)
            out.append(row)
        return _Result(out)

    def _find_indices(self, predicate) -> list[int]:
        return [i for i, r in enumerate(self.store.records(self.table)) if predicate(r)]

    def _do_update(self) -> _Result:
        recs = self.store.records(self.table)
        cols = SCHEMAS[self.table]["columns"]
        updated = []
        for i, r in enumerate(recs):
            if all(_matches(r, c, op, v) for c, op, v in self._filters):
                new = dict(r)
                new.update(self._payload)
                if "updated_at" in cols:
                    new["updated_at"] = _now_iso()
                new = {c: new.get(c) for c in cols}
                self.store.update_at(self.table, i, new)
                updated.append(new)
        return _Result(updated)

    def _conflict_key(self, row: dict[str, Any]) -> tuple:
        keys = [k.strip() for k in (self._on_conflict or "id").split(",")]
        return tuple(str(row.get(k)) for k in keys)

    def _do_upsert(self) -> _Result:
        payloads = self._payload if isinstance(self._payload, list) else [self._payload]
        recs = self.store.records(self.table)
        cols = SCHEMAS[self.table]["columns"]

        # índice de filas existentes por clave de conflicto
        existing: dict[tuple, int] = {}
        for i, r in enumerate(recs):
            existing[self._conflict_key(r)] = i

        out = []
        for p in payloads:
            key = self._conflict_key(p)
            if key in existing:
                if self._ignore_duplicates:
                    continue  # supabase devuelve vacío para filas ignoradas
                idx = existing[key]
                merged = dict(recs[idx])
                merged.update(p)
                if "updated_at" in cols:
                    merged["updated_at"] = _now_iso()
                merged = {c: merged.get(c) for c in cols}
                self.store.update_at(self.table, idx, merged)
                out.append(merged)
            else:
                row = self._prepare_new(p)
                self.store.append(self.table, row)
                existing[self._conflict_key(row)] = len(recs) - 1
                out.append(row)
        return _Result(out)

    # ── helpers de lectura ────────────────────────────────────────────────
    def _do_select(self) -> _Result:
        if self.table == "v_pending_post_candidates":
            rows = _view_pending_candidates(self.store)
        elif "(" in self._select:
            rows = _select_with_joins(self.store, self.table, self._select)
        else:
            rows = [dict(r) for r in self.store.records(self.table)]

        # filtros (soportan columnas con punto: "analyzed_items.primary_slug")
        for col, op, val in self._filters:
            rows = [r for r in rows if _matches(_resolve(r, col), *_split_dotted(r, col, op, val))] \
                if "." in col else [r for r in rows if _matches(r, col, op, val)]

        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)

        if self._limit is not None:
            rows = rows[: self._limit]

        if self._single:
            return _Result(rows[0] if rows else None)
        return _Result(rows)


def _split_dotted(row, col, op, val):
    """Para filtros con punto devuelve (subcol, op, val) sobre el dict anidado."""
    leaf = col.split(".")[-1]
    return (leaf, op, val)


def _resolve(row: dict[str, Any], dotted: str) -> dict[str, Any]:
    """Navega 'analyzed_items.primary_slug' → el dict que contiene 'primary_slug'."""
    parts = dotted.split(".")
    cur: Any = row
    for p in parts[:-1]:
        cur = (cur or {}).get(p) or {}
    return cur if isinstance(cur, dict) else {}


# ===========================================================================
# Joins anidados y vista de candidatos
# ===========================================================================

def _index_by_id(store: _Store, table: str) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in store.records(table) if r.get("id")}


def _attach_chain(store: _Store, base_table: str, row: dict[str, Any], select: str) -> dict[str, Any] | None:
    """Adjunta relaciones anidadas to-one según las que aparezcan en el select."""
    out = dict(row)
    cur_table = base_table
    cur_row = out
    while cur_table in JOINS:
        child_table, fk, pk = JOINS[cur_table]
        if child_table not in select:
            break
        inner = f"{child_table}!inner" in select
        child_idx = _index_by_id(store, child_table)
        child = child_idx.get(cur_row.get(fk))
        if child is None:
            if inner:
                return None  # inner join: descarta la fila sin match
            cur_row[child_table] = {}
            break
        child = dict(child)
        cur_row[child_table] = child
        cur_table = child_table
        cur_row = child
    return out


def _select_with_joins(store: _Store, table: str, select: str) -> list[dict[str, Any]]:
    rows = []
    for r in store.records(table):
        joined = _attach_chain(store, table, r, select)
        if joined is not None:
            rows.append(joined)
    return rows


def _view_pending_candidates(store: _Store) -> list[dict[str, Any]]:
    """Vista v_pending_post_candidates sobre la base de datos curada:

    analyzed_items (con relevance_score >= 0.60) enriquecido con source_name
    vía raw_items ⨝ sources. title/url ya viven en analyzed_items.
    """
    raw_idx = _index_by_id(store, "raw_items")
    src_idx = _index_by_id(store, "sources")

    rows = []
    for ai in store.records("analyzed_items"):
        if (ai.get("relevance_score") or 0) < 0.60:
            continue
        ri = raw_idx.get(ai.get("raw_item_id")) or {}
        s = src_idx.get(ri.get("source_id")) or {}
        rows.append({
            "analyzed_item_id": ai["id"],
            "title": ai.get("title") or ri.get("title"),
            "url": ai.get("url") or ri.get("url"),
            "published_at": ai.get("created_at") or ri.get("published_at"),
            "source_name": s.get("name"),
            "source_type": s.get("type"),
            "summary": ai.get("summary"),
            "key_insights": ai.get("key_insights"),
            "primary_slug": ai.get("primary_slug"),
            "secondary_slug": ai.get("secondary_slug"),
            "keywords": ai.get("keywords"),
            "relevance_score": ai.get("relevance_score"),
            "analyzed_at": ai.get("analyzed_at"),
            "novelty_score": ai.get("novelty_score"),
            "raw_analysis": ai.get("raw_analysis"),
        })

    rows.sort(
        key=lambda r: (
            float(r.get("relevance_score") or 0),
            str(r.get("analyzed_at") or ""),
        ),
        reverse=True,
    )
    return rows


# ===========================================================================
# Cliente
# ===========================================================================

class SheetsClient:
    """Cliente con la misma forma que el de Supabase: .table(...) y .rpc(...)."""

    def __init__(self, spreadsheet=None):
        self._store = _Store(spreadsheet or get_spreadsheet())

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)

    # Compatibilidad con supabase-py
    def from_(self, name: str) -> _Query:
        return self.table(name)

    def rpc(self, name: str, params: dict[str, Any] | None = None) -> _Query:
        # La única RPC usada era search_similar_items (búsqueda vectorial).
        # Sin embeddings devolvemos un resultado vacío de forma controlada.
        q = _Query(self._store, "__rpc__")
        q._op = "rpc"
        q.execute = lambda: _Result([])  # type: ignore[method-assign]
        return q


def get_spreadsheet_client() -> SheetsClient:
    return SheetsClient()
