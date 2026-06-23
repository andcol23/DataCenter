"""
DataCenter — Daily Digest

Envía UN correo diario (HTML) con el top 5 de noticias del día, ordenadas por
relevancia/novedad y con cuota de diversidad por tema. Es puramente informativo:
la base de datos curada vive en analyzed_items; aquí solo se selecciona y maqueta.

Uso:
    python -m output.daily_digest

Env vars:
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN  (scope: gmail.send)
    GOOGLE_SHEET_ID
    DAILY_DIGEST_TO        — destinatario (requerido, sin default)
    MAX_SHORTLIST          — nº de noticias en el correo (default: 5)
    MONDAY_LOOKBACK_HOURS  — override de la ventana del lunes (default: calculada)
    DRY_RUN                — si "true", imprime el correo en vez de enviarlo
"""
from __future__ import annotations

import base64
import html
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any

import structlog
from dotenv import load_dotenv

load_dotenv()

log = structlog.get_logger()

RECIPIENT     = os.environ["DAILY_DIGEST_TO"]
MAX_SHORTLIST = int(os.getenv("MAX_SHORTLIST", "5"))
SHORTLIST_WINDOW_HOURS = int(os.getenv("SHORTLIST_WINDOW_HOURS", "24"))
SHORTLIST_POOL_LIMIT = int(os.getenv("SHORTLIST_POOL_LIMIT", str(max(MAX_SHORTLIST * 10, 50))))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Zona horaria de España para decidir la ventana de lookback según el día.
try:
    from zoneinfo import ZoneInfo
    _SPAIN_TZ = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover - fallback si no hay tzdata
    _SPAIN_TZ = timezone(timedelta(hours=2))

# ── Términos para afinar el ranking (no filtran, solo bonifican) ───────────────

DOOH_TERMS = (
    "dooh", "ooh", "out of home", "out-of-home", "exterior digital", "media exterior",
    "digital signage", "programmatic dooh", "pdooh", "outdoor advertising",
)
DATA_MARKET_TERMS = (
    "warc", "infoadex", "kantar", "magna", "nielsen", "iab", "estudio", "study",
    "forecast", "informe", "report", "adspend", "inversión publicitaria",
)
SPAIN_TERMS = ("spain", "españa", "espana", "mercado español")
RESEARCH_SOURCES = {
    "WARC", "InfoAdex", "Kantar", "Kantar Media", "MAGNA", "Nielsen Insights",
    "eMarketer", "IAB Spain", "IAB (Industry News)", "Think with Google",
}


# ── Ventana de lookback según el día ──────────────────────────────────────────

def _lookback_hours_for_today(now: datetime | None = None) -> int:
    """Lunes: cubre desde el viernes 12:00 España. Resto: ventana estándar de 24h."""
    now = now or datetime.now(_SPAIN_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_SPAIN_TZ)
    now_es = now.astimezone(_SPAIN_TZ)

    if now_es.weekday() == 0:  # lunes
        override = os.getenv("MONDAY_LOOKBACK_HOURS")
        if override:
            return int(override)
        friday_noon = (now_es - timedelta(days=3)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        delta_hours = (now_es - friday_noon).total_seconds() / 3600.0
        return max(SHORTLIST_WINDOW_HOURS, math.ceil(delta_hours))

    return SHORTLIST_WINDOW_HOURS


# ── Gmail (solo envío) ─────────────────────────────────────────────────────────

def _gmail_service() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _send_email(service: Any, to: str, subject: str, html_body: str) -> dict[str, Any]:
    msg = MIMEText(html_body, "html", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("email_sent", to=to, subject=subject, message_id=result.get("id"))
    return result


# ── Selección de candidatos ────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _text_blob(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        " ".join(map(str, item.get("key_insights") or [])),
        " ".join(map(str, item.get("keywords") or [])),
    ]
    return " ".join(parts).lower()


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        if len(term) <= 3 and term.replace("-", "").isalnum():
            if re.search(rf"\b{re.escape(term)}\b", text):
                return True
        elif term in text:
            return True
    return False


def _priority(item: dict[str, Any]) -> float:
    """Puntuación de orden: relevancia + novedad + pequeños bonus editoriales."""
    text = _text_blob(item)
    score = float(item.get("relevance_score") or 0) * 100
    score += float(item.get("novelty_score") or 0) * 35

    if _has_any(text, DOOH_TERMS) or item.get("secondary_slug") == "ooh-dooh":
        score += 20
    if (item.get("source_name") or "") in RESEARCH_SOURCES or _has_any(text, DATA_MARKET_TERMS):
        score += 18
    if _has_any(text, SPAIN_TERMS):
        score += 12
    return score


def _select_with_diversity(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Ronda 1: el mejor item de cada primary_slug. Ronda 2: rellena hasta 2 por tema."""
    ordered = sorted(rows, key=lambda r: r.get("_priority", 0.0), reverse=True)

    selected: list[dict[str, Any]] = []
    per_primary: dict[str, int] = {}
    seen: set[str] = set()

    for cap in (1, 2):
        for row in ordered:
            if len(selected) >= limit:
                break
            rid = row.get("analyzed_item_id")
            if rid in seen:
                continue
            primary = row.get("primary_slug") or "sin-primary"
            if per_primary.get(primary, 0) >= cap:
                continue
            selected.append(row)
            seen.add(rid)
            per_primary[primary] = per_primary.get(primary, 0) + 1
        if len(selected) >= limit:
            break

    return selected[:limit]


def _get_top_items(db: Any, limit: int = MAX_SHORTLIST) -> list[dict[str, Any]]:
    """Top items dentro de la ventana del día, con cuota de diversidad por tema."""
    lookback_hours = _lookback_hours_for_today()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    log.info("shortlist_window", lookback_hours=lookback_hours, cutoff=cutoff_dt.isoformat())

    rows = (
        db.table("v_pending_post_candidates")
        .select("*")
        .gte("analyzed_at", cutoff_dt.isoformat())
        .limit(SHORTLIST_POOL_LIMIT)
        .execute()
    ).data or []

    if not rows:
        log.warning("no_candidates_for_digest")
        return []

    recent = []
    for row in rows:
        item_dt = _parse_dt(row.get("published_at")) or _parse_dt(row.get("analyzed_at"))
        if item_dt and item_dt >= cutoff_dt:
            row["_priority"] = _priority(row)
            recent.append(row)

    return _select_with_diversity(recent, limit)


# ── Maquetación HTML ───────────────────────────────────────────────────────────

def _hashtags(item: dict[str, Any]) -> list[str]:
    """Topics como #hashtags: primary, secondary y la parte slug de cada keyword."""
    tags: list[str] = []
    for slug in (item.get("primary_slug"), item.get("secondary_slug")):
        if slug:
            tags.append(str(slug))
    for kw in item.get("keywords") or []:
        slug = str(kw).split("/", 1)[-1]
        if slug and slug not in tags:
            tags.append(slug)
    return tags[:8]


def _fmt_date(iso: str | None) -> str:
    dt = _parse_dt(iso)
    return dt.strftime("%d %b") if dt else "—"


def _esc(text: Any) -> str:
    return html.escape(str(text or ""))


def _format_email_html(items: list[dict[str, Any]], today_str: str) -> str:
    blocks: list[str] = []
    for i, item in enumerate(items, start=1):
        title = _esc(item.get("title") or "(sin título)")
        url = item.get("url") or ""
        source = _esc(item.get("source_name") or "—")
        pub = _fmt_date(item.get("published_at"))
        summary = _esc(item.get("summary") or "")
        insights = [ _esc(x) for x in (item.get("key_insights") or []) ]
        tags = " ".join(f"#{_esc(t)}" for t in _hashtags(item))

        title_html = (
            f'<a href="{_esc(url)}" style="color:#000000;text-decoration:none;">{title}</a>'
            if url else title
        )

        insights_html = ""
        if insights:
            lis = "".join(
                f'<li style="margin:0 0 4px 0;">{x}</li>' for x in insights
            )
            insights_html = (
                f'<ul style="margin:8px 0;padding-left:20px;color:#444444;'
                f'font-style:italic;font-size:14px;line-height:1.45;">{lis}</ul>'
            )

        link_html = (
            f'<a href="{_esc(url)}" style="color:#1a73e8;font-size:13px;">Leer en {source} →</a>'
            if url else f'<span style="color:#888;font-size:13px;">{source}</span>'
        )

        blocks.append(f"""
        <tr><td style="padding:22px 0 18px 0;border-bottom:1px solid #ececec;">
          <div style="font-size:20px;font-weight:700;color:#000000;line-height:1.3;">
            {i}. {title_html}
          </div>
          <div style="margin:6px 0 0 0;font-size:12px;color:#5ba3e0;">{tags}</div>
          {insights_html}
          <div style="margin:6px 0;font-size:15px;color:#222222;line-height:1.5;">{summary}</div>
          <div style="margin-top:6px;font-size:12px;color:#999999;">
            {source} · {pub} &nbsp;|&nbsp; {link_html}
          </div>
        </td></tr>""")

    body_rows = "".join(blocks)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#ffffff;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#ffffff;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;background:#ffffff;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
                    padding:24px;">
        <tr><td>
          <div style="font-size:26px;font-weight:800;color:#000000;">DataCenter</div>
          <div style="font-size:14px;color:#888888;margin-top:2px;">
            Top {len(items)} · {_esc(today_str)}
          </div>
        </td></tr>
        {body_rows}
        <tr><td style="padding-top:20px;font-size:12px;color:#aaaaaa;">
          Base de datos curada · DataCenter
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# ── Flujo principal ─────────────────────────────────────────────────────────────

def run_daily(db: Any) -> None:
    log.info("daily_digest_start")

    items = _get_top_items(db, limit=MAX_SHORTLIST)
    if not items:
        log.warning("daily_no_candidates")
        print("No hay candidatos para el digest de hoy.")
        return

    today_str = datetime.now(_SPAIN_TZ).strftime("%d %b %Y")
    subject = f"DC · Top {len(items)} · {today_str}"
    body = _format_email_html(items, today_str)

    if DRY_RUN:
        log.info("daily_digest_dry_run", topics=len(items))
        print(f"[DRY_RUN] {subject}\n")
        print(body)
        return

    try:
        service = _gmail_service()
        _send_email(service, RECIPIENT, subject, body)
    except Exception as exc:
        log.error("daily_email_failed", error=str(exc))
        raise

    log.info("daily_digest_done", topics=len(items))
    print(f"Correo enviado: {len(items)} noticias.")


def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ]
    )

    from db.client import get_client
    db = get_client()
    run_daily(db)


if __name__ == "__main__":
    main()
