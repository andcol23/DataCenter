from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from dotenv import load_dotenv
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from db.client import (
    AnalyzedItem,
    get_client,
    get_raw_items_pending_analysis,
    insert_analyzed_item,
    update_raw_item_status,
)

load_dotenv()

log = structlog.get_logger()

DRY_RUN      = os.getenv("DRY_RUN", "false").lower() == "true"
BATCH_SIZE   = int(os.getenv("BATCH_SIZE", "10"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "7"))    # solo analizar items de los últimos N días
MAX_ITEMS_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", "60"))
CURATION_MIN_RELEVANCE = float(os.getenv("CURATION_MIN_RELEVANCE", "0.5"))
MODEL        = "gpt-4o-mini"
REQUIRE_VALID_TAXONOMY = os.getenv("REQUIRE_VALID_TAXONOMY", "true").lower() == "true"

MAX_CONTENT_WORDS = 1_500
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

TAXONOMY_PATH = Path(__file__).parent.parent / "config" / "taxonomy.yml"


def _load_taxonomy() -> dict[str, Any]:
    with open(TAXONOMY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_taxonomy_context(tax: dict[str, Any]) -> tuple[str, set[str], set[str]]:
    lines = ["TAXONOMÍA CONTROLADA (usa SOLO estos slugs):"]
    valid_primaries: set[str] = set()
    valid_secondaries: set[str] = set()

    for p in tax.get("primaries", []):
        ps = p["slug"]
        valid_primaries.add(ps)
        secs = [s["slug"] for s in p.get("secondaries", [])]
        for s in secs:
            valid_secondaries.add(f"{ps}/{s}")
        lines.append(f"  [{ps}] → {' | '.join(secs)}")

    ktypes = list(tax.get("keyword_types", {}).keys())
    lines.append(f"\nkeyword_types válidos: {', '.join(ktypes)}")
    lines.append("Formato keywords: {keyword_type}/{keyword_slug}  ej: company/jcdecaux, metric/ad-recall")

    return "\n".join(lines), valid_primaries, valid_secondaries


_TAXONOMY      = _load_taxonomy()
_TAX_CONTEXT, _VALID_PRIMARIES, _VALID_SECONDARIES = _build_taxonomy_context(_TAXONOMY)


def _expand(slugs: list[str]) -> set[str]:
    out: set[str] = set()
    for s in slugs:
        out.add(s)
        out.add(s.replace("-", " "))
    return out


_mf = _TAXONOMY.get("market_focus", {})
RELEVANT_TERMS: tuple[str, ...] = tuple(
    _expand(_mf.get("core", []) + _mf.get("context", []))
    | {
        "advertising", "publicidad", "media", "medios", "adtech", "martech",
        "campaign", "campaña", "agency", "agencia", "brand", "marca",
        "audience", "audiencia", "programmatic", "programática", "retail media",
        "out of home", "out-of-home", "exterior digital", "signage", "marketing",
    }
)
NOISE_TERMS: tuple[str, ...] = tuple(
    _expand(_mf.get("peripheral", []))
    | {
        "renewable energy", "solar", "blockchain", "cripto", "crypto",
        "ciberseguridad", "cybersecurity", "iot", "document scanning",
        "escaneo de documentos", "ocr", "factura", "invoice",
    }
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        if len(term) <= 3 and term.replace("-", "").isalnum():
            if re.search(rf"\b{re.escape(term)}\b", text):
                return True
        elif term in text:
            return True
    return False


def _prefilter_relevant(raw: dict[str, Any]) -> bool:
    text = " ".join([
        str(raw.get("title") or ""),
        str(raw.get("body_text") or raw.get("body_html") or "")[:4_000],
    ]).lower()
    has_relevant = _contains_any(text, RELEVANT_TERMS)
    has_noise = _contains_any(text, NOISE_TERMS)
    if has_noise and not has_relevant:
        return False
    return True



SYSTEM_PROMPT = """\
Eres un analista de inteligencia de medios para un profesional senior del sector \
publicitario español (11+ años en Exterior digital OOH/DOOH/pDOOH, omnicanalidad, \
AdTech, MarTech, IA aplicada a publicidad, datos y consumo). Foco geográfico: España \
y Europa, sin ignorar movimientos globales relevantes.

Tu trabajo es curar: separar señal de ruido y clasificar con precisión.

Reglas de relevance_score (0-1):
- 0.80+ : tema CORE (OOH/DOOH/pDOOH, programática, adtech, IA aplicada a publicidad, \
retail media, medición de audiencias) o noticia destacada del mercado publicitario español/europeo.
- 0.60-0.79 : tema de CONTEXTO con conexión clara a publicidad/medios.
- < 0.50 : periferia sin ángulo publicitario explícito.
- Mercado de un país solo es relevante si es España, global, Europa, o comparable con España.

Responde ÚNICAMENTE con el JSON solicitado, sin texto adicional ni markdown.
Todos los textos de salida en ESPAÑOL, sea cual sea el idioma del original.

REGLA ABSOLUTA: primary_slug y secondary_slug NUNCA pueden ser null ni vacíos. \
Elige siempre el slug más cercano del catálogo.

{taxonomy_context}"""

USER_PROMPT_TEMPLATE = """\
Analiza el siguiente artículo y devuelve un JSON con esta estructura exacta:

{{
  "resumen": "string — resumen claro en 2-3 frases (máx 60 palabras)",
  "key_insights": ["string", "string", "string"],
  "primary_slug": "string — UNO del catálogo de primarios",
  "secondary_slug": "string — UNO de los secundarios del primario elegido",
  "keywords": ["keyword_type/keyword_slug", "keyword_type/keyword_slug"],
  "relevance_score": 0.0,
  "novelty_score": 0.0
}}

Reglas:
- resumen: 2-3 frases, ejecutivo y concreto. Si citas una cifra, identifica la fuente.
- key_insights: exactamente 3 puntos clave (frases breves).
- primary_slug / secondary_slug: SIEMPRE asigna; el más cercano del catálogo. Null/vacío PROHIBIDO.
- keywords: 3-8 items con formato {{keyword_type}}/{{keyword_slug}}; minúsculas y guiones.
- relevance_score (0-1): según las reglas del system prompt.
- novelty_score (0-1): qué tan FRESCA es la noticia/dato. 1.0 = noticia/dato de hoy; \
0.7 = dato nuevo de un tema conocido; 0.3 = tema ya muy circulado; 0.0 = atemporal.

ARTÍCULO:
Título: {title}
Fuente: {source}
Fecha: {published_at}
URL: {url}

Contenido:
{body}"""


def _openai() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def call_analysis(client: OpenAI, prompt: str) -> tuple[dict[str, Any], int]:
    system = SYSTEM_PROMPT.format(taxonomy_context=_TAX_CONTEXT)
    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=0.2,
        max_tokens=600,
    )
    raw = response.choices[0].message.content or "{}"
    tokens = response.usage.total_tokens if response.usage else 0
    return json.loads(raw), tokens



def _truncate(text: str | None) -> str:
    if not text:
        return ""
    words = text.split()
    if len(words) <= MAX_CONTENT_WORDS:
        return text
    return " ".join(words[:MAX_CONTENT_WORDS]) + "\n[... truncado ...]"


def _build_prompt(raw: dict[str, Any]) -> str:
    body = _truncate(raw.get("body_text") or raw.get("body_html") or "")
    return USER_PROMPT_TEMPLATE.format(
        title=raw.get("title") or "(sin título)",
        source=raw.get("metadata", {}).get("feed_url", "desconocida"),
        published_at=raw.get("published_at") or "desconocida",
        url=raw.get("url") or "",
        body=body or "(contenido no disponible)",
    )


@dataclasses.dataclass(frozen=True)
class TaxonomyValidation:
    primary_slug: str | None
    secondary_slug: str | None
    keywords: list[str]
    errors: list[str]


def _validate_taxonomy(data: dict[str, Any]) -> TaxonomyValidation:
    primary = str(data.get("primary_slug") or "").strip().lower()
    secondary = str(data.get("secondary_slug") or "").strip().lower()
    raw_keywords = data.get("keywords") or []
    errors: list[str] = []

    if primary not in _VALID_PRIMARIES:
        log.warning("taxonomy_invalid_primary", got=primary, valid=sorted(_VALID_PRIMARIES))
        errors.append(f"primary_slug inválido: {primary or '(vacío)'}")
        primary = None

    if primary and f"{primary}/{secondary}" not in _VALID_SECONDARIES:
        log.warning("taxonomy_invalid_secondary", primary=primary, got=secondary)
        errors.append(f"secondary_slug inválido para {primary}: {secondary or '(vacío)'}")
        secondary = None
    elif not primary:
        secondary = None

    valid_ktypes = set(_TAXONOMY.get("keyword_types", {}).keys())
    clean_keywords: list[str] = []
    seen_keywords: set[str] = set()
    for kw in raw_keywords[:10]:
        kw_str = str(kw).strip().lower()
        parts = kw_str.split("/", 1)
        if len(parts) != 2:
            log.debug("taxonomy_invalid_keyword", kw=kw)
            continue

        kw_type, kw_slug = parts
        if kw_type not in valid_ktypes or not SLUG_PATTERN.fullmatch(kw_slug):
            log.debug("taxonomy_invalid_keyword", kw=kw)
            continue

        canonical = f"{kw_type}/{kw_slug}"
        if canonical in seen_keywords:
            continue
        seen_keywords.add(canonical)
        clean_keywords.append(canonical)

    if len(clean_keywords) < 1:
        errors.append(f"keywords válidas insuficientes: {len(clean_keywords)}")

    return TaxonomyValidation(
        primary_slug=primary,
        secondary_slug=secondary,
        keywords=clean_keywords[:8],
        errors=errors,
    )


def _clamp01(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _to_analyzed_item(
    data: dict[str, Any],
    raw: dict[str, Any],
    tokens: int,
    taxonomy: TaxonomyValidation,
    published_at: datetime | None = None,
) -> AnalyzedItem:
    insights = [str(i) for i in (data.get("key_insights") or []) if str(i).strip()][:5]
    return AnalyzedItem(
        raw_item_id=UUID(raw["id"]),
        title=raw.get("title") or "",
        url=raw.get("url"),
        summary=data.get("resumen", ""),
        key_insights=insights,
        primary_slug=taxonomy.primary_slug,
        secondary_slug=taxonomy.secondary_slug,
        keywords=taxonomy.keywords,
        relevance_score=_clamp01(data.get("relevance_score")) or 0.5,
        novelty_score=_clamp01(data.get("novelty_score")),
        raw_analysis=data,
        model_used=MODEL,
        tokens_used=tokens,
        published_at=published_at,
    )



def process_item(raw: dict[str, Any], oai: OpenAI, db: Any) -> bool:
    item_id = raw["id"]
    title   = (raw.get("title") or "")[:80]

    content = raw.get("body_text") or raw.get("body_html") or ""
    if not content.strip():
        log.warning("no_content", item_id=item_id, title=title)
        if not DRY_RUN:
            update_raw_item_status(db, item_id, "failed", "No content to analyze")
        return False

    if not _prefilter_relevant(raw):
        log.info("item_prefiltered", title=title)
        if not DRY_RUN:
            update_raw_item_status(db, item_id, "archived", "prefilter: sin ángulo de medios")
        return True

    if not DRY_RUN:
        update_raw_item_status(db, item_id, "analyzing")

    try:
        data, tokens = call_analysis(oai, _build_prompt(raw))
    except Exception as exc:
        log.error("analysis_api_error", item_id=item_id, error=str(exc))
        if not DRY_RUN:
            update_raw_item_status(db, item_id, "failed", str(exc)[:500])
        return False

    missing = [f for f in ("resumen", "key_insights", "relevance_score") if not data.get(f)]
    if missing:
        log.error("missing_fields", item_id=item_id, missing=missing)
        if not DRY_RUN:
            update_raw_item_status(db, item_id, "failed", f"Missing fields: {missing}")
        return False

    taxonomy = _validate_taxonomy(data)
    if REQUIRE_VALID_TAXONOMY and taxonomy.errors:
        log.error("invalid_taxonomy", item_id=item_id, errors=taxonomy.errors)
        if not DRY_RUN:
            update_raw_item_status(db, item_id, "failed", "; ".join(taxonomy.errors)[:500])
        return False

    pub_at_raw = raw.get("published_at")
    pub_at: datetime | None = None
    if pub_at_raw:
        try:
            pub_at = datetime.fromisoformat(str(pub_at_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pub_at = None

    analyzed = _to_analyzed_item(data, raw, tokens, taxonomy, published_at=pub_at)

    if DRY_RUN:
        log.info("dry_run_result",
                 title=title,
                 primary=taxonomy.primary_slug,
                 secondary=taxonomy.secondary_slug,
                 keywords=taxonomy.keywords[:4],
                 relevance=data.get("relevance_score"),
                 novelty=data.get("novelty_score"),
                 tokens=tokens)
        return True

    if analyzed.relevance_score < CURATION_MIN_RELEVANCE:
        log.info("item_archived_low_relevance", title=title, score=analyzed.relevance_score)
        update_raw_item_status(db, item_id, "archived", f"relevance {analyzed.relevance_score} < {CURATION_MIN_RELEVANCE}")
        return True

    insert_analyzed_item(db, analyzed)
    update_raw_item_status(db, item_id, "analyzed")
    log.info("item_analyzed", title=title,
             score=analyzed.relevance_score,
             novelty=analyzed.novelty_score,
             primary=analyzed.primary_slug,
             tokens=tokens)
    return True



def run_all(oai: OpenAI, db: Any) -> None:
    total_ok = total_fail = batch_n = total_seen = 0

    while True:
        if DRY_RUN:
            items = _dry_run_samples()
        else:
            remaining = MAX_ITEMS_PER_RUN - total_seen
            if remaining <= 0:
                log.info("max_items_per_run_reached", limit=MAX_ITEMS_PER_RUN)
                break
            items = get_raw_items_pending_analysis(
                db, limit=min(BATCH_SIZE, remaining), max_age_days=MAX_AGE_DAYS
            )

        if not items:
            log.info("no_pending_items")
            break

        batch_n += 1
        total_seen += len(items)
        log.info("batch_start", batch=batch_n, items=len(items))

        ok = fail = 0
        for i, raw in enumerate(items):
            if process_item(raw, oai, db):
                ok += 1
            else:
                fail += 1
            if i < len(items) - 1:
                time.sleep(0.5)

        total_ok   += ok
        total_fail += fail
        log.info("batch_done", batch=batch_n, ok=ok, failed=fail)

        if DRY_RUN or len(items) < BATCH_SIZE:
            break

        time.sleep(1)

    log.info("analyzer_done", batches=batch_n, total_ok=total_ok, total_failed=total_fail)



def _dry_run_samples() -> list[dict[str, Any]]:
    return [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": "JCDecaux y Broadsign amplían su oferta programática DOOH en España",
            "url": "https://example.com/jcdecaux-pdooh-spain",
            "published_at": "2026-05-28T10:00:00Z",
            "body_text": (
                "JCDecaux ha anunciado la expansión de su inventario programático de "
                "Exterior digital (pDOOH) en el mercado español, integrándose con nuevas "
                "DSP. La compañía apunta a que la compra programática de DOOH crece a doble "
                "dígito y permite a las marcas activar campañas por audiencia y contexto en "
                "tiempo real, acercando el medio Exterior a las lógicas de performance digital."
            ),
            "metadata": {"feed_url": "https://example.com/feed/"},
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "title": "España lidera la inversión en energía solar renovable en 2026",
            "url": "https://example.com/solar-spain",
            "published_at": "2026-05-27T14:00:00Z",
            "body_text": (
                "El sector de la energía solar en España creció un 18% en el último año, "
                "impulsado por nuevas inversiones en producción renovable y apoyo regulatorio."
            ),
            "metadata": {"feed_url": "https://example.com/feed/"},
        },
    ]



def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ]
    )

    log.info(
        "analyzer_start",
        dry_run=DRY_RUN,
        batch_size=BATCH_SIZE,
        max_age_days=MAX_AGE_DAYS,
        max_items_per_run=MAX_ITEMS_PER_RUN,
        curation_min_relevance=CURATION_MIN_RELEVANCE,
        model=MODEL,
        require_valid_taxonomy=REQUIRE_VALID_TAXONOMY,
    )

    oai = _openai()
    db  = get_client() if not DRY_RUN else None

    run_all(oai, db)


if __name__ == "__main__":
    main()
