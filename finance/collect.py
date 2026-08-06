"""Оркестратор: запускает все коллекторы, кладёт в SQLite."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from .collectors import collect_edisclosure, collect_moex, collect_rss
from .config import NEWS_WINDOW_HOURS
from .storage import connect, init_db, upsert_news
from .tickers import refresh as refresh_tickers

log = logging.getLogger("collect")


async def _run(window_hours: int, refresh_ticks: bool) -> dict:
    init_db()
    if refresh_ticks:
        log.info("Обновляю справочник тикеров…")
        n = await refresh_tickers()
        log.info("Тикеров в базе: %d", n)

    since_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    log.info("Окно сбора: с %s UTC", since_dt.isoformat(timespec="minutes"))

    stats: dict[str, dict] = {}

    # RSS
    rss_map = await collect_rss(since_dt)
    for src, items in rss_map.items():
        ins, dup = upsert_news(items)
        stats[src] = {"fetched": len(items), "new": ins, "dup": dup}

    # MOEX ISS
    try:
        moex_items = await collect_moex(since_dt)
    except Exception as e:
        log.warning("MOEX collector failed: %s", e)
        moex_items = []
    ins, dup = upsert_news(moex_items)
    stats["moex"] = {"fetched": len(moex_items), "new": ins, "dup": dup}

    # e-disclosure
    try:
        ed_items = await collect_edisclosure(since_dt)
    except Exception as e:
        log.warning("e-disclosure collector failed: %s", e)
        ed_items = []
    ins, dup = upsert_news(ed_items)
    stats["edisclosure"] = {"fetched": len(ed_items), "new": ins, "dup": dup}

    # запись run
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, stats) VALUES (?, ?, ?)",
            (
                since_dt.isoformat(timespec="seconds"),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(stats, ensure_ascii=False),
            ),
        )
        conn.commit()

    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=NEWS_WINDOW_HOURS)
    ap.add_argument("--no-tickers", action="store_true", help="Не обновлять справочник тикеров")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    stats = asyncio.run(_run(args.hours, refresh_ticks=not args.no_tickers))
    log.info("Готово. Статистика:\n%s", json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
