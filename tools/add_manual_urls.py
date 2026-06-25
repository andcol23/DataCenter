from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import structlog

from db.client import RawItem, get_client, upsert_raw_items, upsert_source

log = structlog.get_logger()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
URL_RE = re.compile(r"https?://[^\s<>'\"]+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
OG_TITLE_RE = re.compile(
    r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)


def parse_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,);]")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def extract_title(raw_html: str, fallback_url: str) -> str:
    og = OG_TITLE_RE.search(raw_html)
    if og:
        return _clean_html_text(og.group(1))
    title = TITLE_RE.search(raw_html)
    if title:
        return _clean_html_text(re.sub(r"<[^>]+>", " ", title.group(1)))
    return fallback_url


def html_to_text(raw_html: str) -> str:
    import html2text

    h2t = html2text.HTML2Text()
    h2t.ignore_links = False
    h2t.ignore_images = True
    h2t.body_width = 0
    text = h2t.handle(raw_html).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def fetch_manual_url(url: str) -> dict[str, Any]:
    headers = {"User-Agent": "MediaIntelligenceHub/1.0 (manual URL importer; +github.com)"}
    response = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()
    raw_html = response.text
    final_url = str(response.url)
    title = extract_title(raw_html, final_url)
    body_text = html_to_text(raw_html)
    if len(body_text) > 45_000:
        body_text = body_text[:45_000] + "\n...[truncado]"
    return {
        "url": final_url,
        "title": title,
        "body_text": body_text or title,
        "metadata": {
            "manual_url": True,
            "submitted_url": url,
            "content_type": response.headers.get("content-type"),
            "imported_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def external_id_for_url(url: str) -> str:
    return "manual:" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def build_items(urls: list[str], source_id: UUID) -> tuple[list[RawItem], int]:
    items: list[RawItem] = []
    failed = 0
    for url in urls:
        try:
            data = fetch_manual_url(url)
            items.append(
                RawItem(
                    source_id=source_id,
                    external_id=external_id_for_url(data["url"]),
                    url=data["url"],
                    title=data["title"],
                    body_text=data["body_text"],
                    published_at=datetime.now(timezone.utc),
                    metadata=data["metadata"],
                )
            )
            log.info("manual_url_fetched", url=url, title=data["title"])
        except Exception as exc:
            failed += 1
            log.error("manual_url_fetch_failed", url=url, error=str(exc))
    return items, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Add manual URLs to raw_items for analysis.")
    parser.add_argument("urls", nargs="*", help="URLs to import")
    parser.add_argument("--file", help="Text file with URLs, one per line or pasted in any text")
    parser.add_argument("--stdin", action="store_true", help="Read URLs from stdin")
    args = parser.parse_args()

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ]
    )

    chunks = ["\n".join(args.urls)]
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            chunks.append(f.read())
    if args.stdin:
        chunks.append(sys.stdin.read())

    urls = parse_urls("\n".join(chunks))
    log.info("manual_urls_start", count=len(urls), dry_run=DRY_RUN)
    if not urls:
        raise SystemExit("No URLs found.")

    db = None if DRY_RUN else get_client()
    source_row = {"id": "00000000-0000-0000-0000-000000000000"}
    if db:
        source_row = upsert_source(
            db,
            "Manual URLs",
            "manual",
            {"input": "workflow_dispatch_or_cli"},
            is_active=True,
        )

    items, failed = build_items(urls, UUID(source_row["id"]))
    if DRY_RUN:
        for item in items:
            log.info("manual_url_dry_run_item", title=item.title, url=item.url)
        inserted = len(items)
    elif db and items:
        inserted = len(upsert_raw_items(db, items))
    else:
        inserted = 0

    log.info(
        "manual_urls_done",
        found=len(urls),
        fetched=len(items),
        inserted=inserted,
        duplicates=len(items) - inserted if not DRY_RUN else 0,
        failed=failed,
    )
    if failed and not items:
        raise RuntimeError("No manual URLs could be fetched.")


if __name__ == "__main__":
    main()
