from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _first_row_from_grid_metadata(ss, worksheet) -> list[str]:
    metadata = ss.fetch_sheet_metadata(
        params={"includeGridData": "true", "ranges": [f"'{worksheet.title}'!A1:Z1"]}
    )
    sheet = next(
        (
            s
            for s in metadata.get("sheets", [])
            if s.get("properties", {}).get("sheetId") == worksheet.id
        ),
        None,
    )
    row_data = ((sheet or {}).get("data") or [{}])[0].get("rowData") or []
    cells = row_data[0].get("values", []) if row_data else []
    values: list[str] = []
    for cell in cells:
        effective = cell.get("effectiveValue") or {}
        values.append(str(next(iter(effective.values()), "")))
    return values


def _credentials():
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"].strip(),
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"].strip(),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
    )


def _print_refresh_token_scopes() -> None:
    import requests

    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"].strip(),
        },
        timeout=30,
    )
    if not r.ok:
        print(f"\n✗ No se pudo refrescar el token de Google ({r.status_code}).")
        print(f"  Respuesta: {r.text[:500]}")
        sys.exit(1)

    granted_scope = r.json().get("scope", "")
    print(f"  Scopes concedidos por el token: {granted_scope or '(no devuelto)'}")
    if granted_scope and SHEETS_SCOPE not in set(granted_scope.split()):
        print("\n✗ El GOOGLE_REFRESH_TOKEN no incluye el scope de Google Sheets.")
        print(f"  Falta: {SHEETS_SCOPE}")
        print("  → Regenera GOOGLE_REFRESH_TOKEN con `python tools/google_auth.py`")
        print("    o usa GOOGLE_SERVICE_ACCOUNT_JSON en GitHub Actions.")
        sys.exit(1)


def _open_spreadsheet(sheet_id: str):
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
            body = response.text[:1000] if response.text else "(empty response body)"
            raise RuntimeError(
                f"Sheets API error {response.status_code} [{method.upper()} {endpoint}]: {body!r}"
            )

    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if service_account_json:
        try:
            service_account_info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            print("\n✗ GOOGLE_SERVICE_ACCOUNT_JSON no es JSON válido.")
            print(f"  Motivo: {exc}")
            sys.exit(1)

        email = service_account_info.get("client_email", "(sin client_email)")
        print(f"  Modo auth: service account")
        print(f"  Cuenta autorizada: {email}")
        gc = gspread.service_account_from_dict(
            service_account_info,
            http_client=_SheetsHTTPClient,
        )
        return gc.open_by_key(sheet_id), email

    print("  Modo auth: OAuth refresh token")
    _print_refresh_token_scopes()
    creds = _credentials()

    try:
        import google.auth.transport.requests as gar
        creds.refresh(gar.Request())
        import urllib.request
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        with urllib.request.urlopen(req) as r:
            info = json.loads(r.read().decode())
        email = info.get("email", "(desconocido)")
        print(f"  Cuenta autorizada por el token: {email}")
    except Exception as exc:
        email = "(no se pudo determinar)"
        print(f"  Aviso: no se pudo leer el email de la cuenta ({exc})")
        print("  (Puede que falte el scope userinfo.email; no es bloqueante.)")

    gc = gspread.Client(auth=creds, http_client=_SheetsHTTPClient)
    return gc.open_by_key(sheet_id), email


def main() -> None:
    print("\n=== Verificación de acceso a Google Sheets ===\n")

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        print("✗ Falta GOOGLE_SHEET_ID en .env")
        sys.exit(1)

    email = "(desconocida)"
    try:
        ss, email = _open_spreadsheet(sheet_id)
        print(f"  ✓ Spreadsheet abierto: '{ss.title}'")
        tabs = [ws.title for ws in ss.worksheets()]
        print(f"    Pestañas actuales ({len(tabs)}): {tabs}")
        for required_tab in ("sources", "raw_items", "analyzed_items"):
            ws = ss.worksheet(required_tab)
            header = _first_row_from_grid_metadata(ss, ws)
            if not header:
                raise RuntimeError(f"La pestaña {required_tab!r} no tiene cabecera en fila 1.")
            print(f"    ✓ Lectura OK: {required_tab} ({len(header)} columnas)")
    except Exception as exc:
        print(f"\n✗ NO se pudo abrir el spreadsheet.")
        print(f"  Motivo: {exc}")
        print(f"\n  → La cuenta '{email}' probablemente NO tiene acceso a este Sheet,")
        print(f"    o el GOOGLE_SHEET_ID es incorrecto.")
        print(f"  → Comparte el Sheet con esa cuenta (permiso Editor), o re-autoriza")
        print(f"    con `python tools/google_auth.py` usando la cuenta correcta.")
        sys.exit(1)

    try:
        tmp_title = f"__verify_tmp_{os.getenv('GITHUB_RUN_ID', os.getpid())}__"
        tmp = ss.add_worksheet(title=tmp_title, rows=2, cols=2)
        ss.batch_update(
            {
                "requests": [
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": tmp.id,
                                "rowIndex": 0,
                                "columnIndex": 0,
                            },
                            "rows": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"stringValue": "ok"}}
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue",
                        }
                    }
                ]
            }
        )
        ss.del_worksheet(tmp)
        print("  ✓ Permiso de ESCRITURA confirmado (creó/escribió/borró una pestaña de prueba)")
    except Exception as exc:
        print(f"\n✗ La cuenta puede LEER pero NO ESCRIBIR.")
        print(f"  Motivo: {exc}")
        print(f"  → Dale permiso de **Editor** (no solo Lector) a '{email}' en el Sheet.")
        sys.exit(1)

    print("\n✓ TODO OK — el token puede leer y escribir. Ya puedes migrar:\n")
    print("    python migrate_supabase_to_sheets.py\n")


if __name__ == "__main__":
    main()
