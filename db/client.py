"""
Cliente de datos sobre Google Sheets + helpers de acceso.

Antes esto hablaba con Supabase/Postgres; ahora el backend es Google Sheets
(ver db/sheets_backend.py). La API pública (get_client, upsert_raw_item, etc.)
se mantiene idéntica para no tocar el resto del pipeline.

Cambios respecto a Supabase:
  • Se eliminó la columna `embedding` de analyzed_items (búsqueda vectorial fuera).
  • Se eliminó `body_html` de raw_items.
  • `fetch_logs` ya no se persiste (log_fetch es un no-op que solo escribe en stdout).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from db.sheets_backend import SheetsClient, get_spreadsheet_client

load_dotenv()

# Alias de tipo para no romper anotaciones `db: Client` repartidas por el código.
Client = SheetsClient


def get_client() -> SheetsClient:
    return get_spreadsheet_client()


# ---------------------------------------------------------------------------
# Dataclasses (mirrors de las tablas principales)
# ---------------------------------------------------------------------------

@dataclass
class RawItem:
    source_id: UUID
    external_id: str
    url: str | None = None
    title: str | None = None
    body_text: str | None = None
    body_html: str | None = None      # se conserva en memoria pero NO se guarda en Sheets
    author: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "raw"

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "body_text": self.body_text,
            # body_html intencionadamente omitido (excluido del backend Sheets)
            "author": self.author,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "metadata": self.metadata,
            "status": self.status,
        }


@dataclass
class AnalyzedItem:
    """Registro de la base de datos curada (analyzed_items).

    Esquema reducido para abaratar el análisis: solo lo necesario para curar,
    rankear el top 5 y permitir correlaciones (resumen, insights, taxonomía,
    keywords, relevancia y novedad). Se denormalizan `title` y `url` para que
    la pestaña sea autónoma y legible sin joins.
    """
    raw_item_id: UUID
    title: str
    url: str | None
    summary: str
    key_insights: list[str]
    primary_slug: str | None
    secondary_slug: str | None
    keywords: list[str]
    relevance_score: float
    novelty_score: float | None
    raw_analysis: dict[str, Any]
    model_used: str = "gpt-4o-mini"
    tokens_used: int | None = None
    published_at: datetime | None = None  # fecha de publicación original del raw_item

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "raw_item_id": str(self.raw_item_id),
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "key_insights": self.key_insights,
            "primary_slug": self.primary_slug,
            "secondary_slug": self.secondary_slug,
            "keywords": self.keywords,
            "relevance_score": round(float(self.relevance_score), 2),
            "novelty_score": round(float(self.novelty_score), 2) if self.novelty_score is not None else None,
            "raw_analysis": self.raw_analysis,
            "model_used": self.model_used,
            "tokens_used": self.tokens_used,
            # created_at = fecha de publicación original; updated_at/analyzed_at = now() por defecto
            "created_at": self.published_at.isoformat() if self.published_at else None,
            "analyzed_at": _now_iso(),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers de DB
# ---------------------------------------------------------------------------

def upsert_raw_item(db: Client, item: RawItem) -> dict[str, Any] | None:
    """Inserta o ignora duplicados (source_id, external_id). Devuelve la fila."""
    result = (
        db.table("raw_items")
        .upsert(item.to_db_dict(), on_conflict="source_id,external_id", ignore_duplicates=True)
        .execute()
    )
    return result.data[0] if result.data else None


def get_raw_items_pending_analysis(
    db: Client, limit: int = 50, max_age_days: int = 7
) -> list[dict[str, Any]]:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    result = (
        db.table("raw_items")
        .select("*")
        .eq("status", "raw")
        .gte("created_at", cutoff)
        .order("published_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def update_raw_item_status(db: Client, item_id: str, status: str, error: str | None = None) -> None:
    payload: dict[str, Any] = {"status": status}
    if error:
        payload["error_message"] = error
    db.table("raw_items").update(payload).eq("id", item_id).execute()


def insert_analyzed_item(db: Client, item: AnalyzedItem) -> dict[str, Any] | None:
    """Inserta el análisis. Si ya existe uno para ese raw_item, marca el raw como
    'analyzed' y no duplica (en Postgres lo garantizaba la UNIQUE de raw_item_id)."""
    raw_item_id = str(item.raw_item_id)
    existing = (
        db.table("analyzed_items")
        .select("id")
        .eq("raw_item_id", raw_item_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        update_raw_item_status(db, raw_item_id, "analyzed")
        return None

    result = db.table("analyzed_items").insert(item.to_db_dict()).execute()
    return result.data[0] if result.data else None


def upsert_source(
    db: Client,
    name: str,
    source_type: str,
    config: dict[str, Any],
    *,
    is_active: bool = True,
) -> dict[str, Any]:
    """Inserta o actualiza una fuente por (name, type). Devuelve la fila."""
    result = (
        db.table("sources")
        .upsert(
            {"name": name, "type": source_type, "config": config, "is_active": is_active},
            on_conflict="name,type",
        )
        .execute()
    )
    return result.data[0]


def get_source_by_name(db: Client, name: str) -> dict[str, Any] | None:
    result = db.table("sources").select("*").eq("name", name).eq("is_active", True).limit(1).execute()
    return result.data[0] if result.data else None


def log_fetch(
    db: Client,
    source_id: str | None,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    items_found: int,
    items_new: int,
    items_failed: int,
    success: bool,
    error_message: str | None = None,
) -> None:
    """fetch_logs ya no se persiste en Sheets. Se mantiene la firma y se registra
    un resumen por stdout para no perder visibilidad operativa."""
    import structlog
    structlog.get_logger().info(
        "fetch_log",
        source_id=source_id,
        run_id=run_id,
        items_found=items_found,
        items_new=items_new,
        items_failed=items_failed,
        success=success,
        error_message=error_message,
    )
