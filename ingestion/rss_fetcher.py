from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import feedparser
import httpx
import structlog
import yaml
from tenacity import retry, stop_after_attempt, wait_exponential

from db.client import (
    RawItem,
    get_client,
    log_fetch,
    upsert_raw_items,
    upsert_source,
)

log = structlog.get_logger()

SOURCES_CONFIG = Path(__file__).parent.parent / "config" / "sources.yml"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
RUN_ID = os.getenv("GITHUB_RUN_ID", "local")


def load_rss_sources() -> list[dict[str, Any]]:
    with open(SOURCES_CONFIG) as f:
        config = yaml.safe_load(f)
    return config.get("rss_feeds", [])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_feed(url: str) -> feedparser.FeedParserDict:
    headers = {"User-Agent": "MediaIntelligenceHub/1.0 (RSS fetcher; +github.com)"}
    response = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return feedparser.parse(response.text)


def entry_to_external_id(entry: Any, feed_url: str) -> str:
    if getattr(entry, "id", None):
        return entry.id
    if getattr(entry, "link", None):
        return entry.link
    raw = f"{feed_url}::{entry.get('title', '')}::{entry.get('published', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_published_at(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            import time
            return datetime(*t[:6], tzinfo=timezone.utc)
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                return parsedate_to_datetime(s).astimezone(timezone.utc)
            except Exception:
                pass
    return None


def extract_body(entry: Any) -> tuple[str | None, str | None]:
    html = None
    text = None

    content = getattr(entry, "content", None)
    if content:
        html = content[0].get("value", "")

    summary = getattr(entry, "summary", None)
    if summary:
        if not html:
            html = summary
        import html2text
        h2t = html2text.HTML2Text()
        h2t.ignore_links = False
        h2t.body_width = 0
        text = h2t.handle(html).strip()

    return text, html


def sync_rss_sources(db: Any, source_configs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not db:
        return {}

    source_rows: dict[str, dict[str, Any]] = {}
    for source_config in source_configs:
        name = source_config["name"]
        source_rows[name] = upsert_source(
            db,
            name,
            "rss",
            {
                "url": source_config["url"],
                "min_relevance": source_config.get("min_relevance", 0.60),
            },
            is_active=source_config.get("active", True),
        )
    return source_rows


def process_feed(source_config: dict[str, Any], db: Any, source_row: dict[str, Any] | None = None) -> dict[str, int]:
    name = source_config["name"]
    url = source_config["url"]
    stats = {"found": 0, "new": 0, "failed": 0}
    started_at = datetime.now(timezone.utc)

    log.info("fetching_feed", source=name, url=url)

    if not source_row:
        source_row = {"id": "00000000-0000-0000-0000-000000000000"}

    source_id = UUID(source_row["id"])

    try:
        feed = fetch_feed(url)
    except Exception as e:
        log.error("feed_fetch_failed", source=name, error=str(e))
        if db:
            log_fetch(
                db, str(source_id), RUN_ID, started_at,
                datetime.now(timezone.utc), 0, 0, 1, False, str(e)
            )
        return stats

    entries = feed.entries
    stats["found"] = len(entries)
    log.info("entries_found", source=name, count=len(entries))

    pending_items: list[RawItem] = []
    for entry in entries:
        try:
            external_id = entry_to_external_id(entry, url)
            body_text, body_html = extract_body(entry)

            item = RawItem(
                source_id=source_id,
                external_id=external_id,
                url=getattr(entry, "link", None),
                title=getattr(entry, "title", None),
                body_text=body_text,
                body_html=body_html,
                author=getattr(entry, "author", None),
                published_at=parse_published_at(entry),
                metadata={
                    "feed_url": url,
                    "tags": [t.get("term") for t in getattr(entry, "tags", []) if t.get("term")],
                },
            )

            if DRY_RUN:
                log.info("dry_run_item", title=item.title, url=item.url)
                stats["new"] += 1
                continue

            pending_items.append(item)

        except Exception as e:
            stats["failed"] += 1
            log.error("entry_processing_failed", error=str(e), entry_id=getattr(entry, "id", "?"))

    if db and pending_items:
        try:
            inserted_items = upsert_raw_items(db, pending_items)
            stats["new"] += len(inserted_items)
            log.info(
                "feed_items_saved",
                source=name,
                attempted=len(pending_items),
                inserted=len(inserted_items),
                duplicates=len(pending_items) - len(inserted_items),
            )
        except Exception as e:
            stats["failed"] += len(pending_items)
            log.error("feed_items_save_failed", source=name, count=len(pending_items), error=str(e))

    if db:
        log_fetch(
            db, str(source_id), RUN_ID, started_at,
            datetime.now(timezone.utc),
            stats["found"], stats["new"], stats["failed"],
            success=True,
        )

    return stats


def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ]
    )

    source_configs = load_rss_sources()
    active_sources = [s for s in source_configs if s.get("active", True)]
    log.info(
        "rss_fetcher_start",
        sources_total=len(source_configs),
        sources_active=len(active_sources),
        dry_run=DRY_RUN,
    )

    db = get_client() if not DRY_RUN else None
    source_rows = sync_rss_sources(db, source_configs)

    total = {"found": 0, "new": 0, "failed": 0}
    for source in active_sources:
        stats = process_feed(source, db, source_rows.get(source["name"]))
        for k in total:
            total[k] += stats[k]

    log.info("rss_fetcher_done", **total)
    if not DRY_RUN and total["found"] > 0 and total["new"] == 0 and total["failed"] > 0:
        raise RuntimeError(
            f"RSS fetch finished without saved items: found={total['found']} failed={total['failed']}"
        )


if __name__ == "__main__":
    main()
