from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from db.sheets_backend import SheetsClient, get_spreadsheet_client

load_dotenv()

Client = SheetsClient


def get_client() -> SheetsClient:
    return get_spreadsheet_client()



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
            "author": self.author,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "metadata": self.metadata,
            "status": self.status,
        }


@dataclass
class AnalyzedItem:
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
            "created_at": self.published_at.isoformat() if self.published_at else None,
            "analyzed_at": _now_iso(),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def upsert_raw_item(db: Client, item: RawItem) -> dict[str, Any] | None:
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
