from __future__ import annotations

import sys
import time
from typing import Any

import structlog

log = structlog.get_logger()


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ]
    )


def _timed(label: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    log.info(f"{label}_start")
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    elapsed = round(time.monotonic() - t0, 1)
    log.info(f"{label}_done", elapsed_s=elapsed)
    return result


def cmd_fetch() -> None:
    from ingestion.rss_fetcher import main as rss_main
    _timed("rss_fetcher", rss_main)


def cmd_analyze() -> None:
    from analysis.analyzer import main as analyzer_main
    _timed("analyzer", analyzer_main)


def cmd_status() -> None:
    try:
        from db.client import get_client
        db = get_client()

        raw_stats = (
            db.table("raw_items")
            .select("status")
            .execute()
        ).data or []

        analyzed_stats = (
            db.table("analyzed_items")
            .select("primary_slug, relevance_score")
            .execute()
        ).data or []

        raw_counts: dict[str, int] = {}
        for row in raw_stats:
            s = row.get("status", "unknown")
            raw_counts[s] = raw_counts.get(s, 0) + 1

        analyzed_primary: dict[str, int] = {}
        for row in analyzed_stats:
            p = row.get("primary_slug") or "sin-primary"
            analyzed_primary[p] = analyzed_primary.get(p, 0) + 1

        print("\n" + "=" * 50)
        print("DataCenter — Estado de la base de datos")
        print("=" * 50)
        print("\n[raw_items]")
        for status, count in sorted(raw_counts.items()):
            print(f"  {status:<15} {count:>5}")

        print(f"\n[analyzed_items]  total: {len(analyzed_stats)}")
        print("  Por primary_slug:")
        for p, count in sorted(analyzed_primary.items(), key=lambda kv: -kv[1]):
            print(f"    {p:<22} {count:>5}")
        print()

    except Exception as exc:
        log.error("status_error", error=str(exc))
        print(f"Error al conectar con la base de datos: {exc}")
        sys.exit(1)


def cmd_run() -> None:
    log.info("pipeline_run_start")
    t0 = time.monotonic()

    cmd_fetch()
    cmd_analyze()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("pipeline_run_done", total_elapsed_s=elapsed)


COMMANDS = {
    "run":      (cmd_run,      "Pipeline completo: fetch + analyze"),
    "fetch":    (cmd_fetch,    "Ingesta RSS"),
    "analyze":  (cmd_analyze,  "Análisis OpenAI de raw_items pendientes"),
    "status":   (cmd_status,   "Estadísticas de la base de datos curada"),
}


def print_help() -> None:
    print(__doc__)
    print("Comandos disponibles:")
    for cmd, (_, desc) in COMMANDS.items():
        print(f"  {cmd:<10} {desc}")
    print()


def main() -> None:
    _configure_logging()

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command in COMMANDS:
        fn, _ = COMMANDS[command]
        fn()
    else:
        print(f"Comando desconocido: '{command}'")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
