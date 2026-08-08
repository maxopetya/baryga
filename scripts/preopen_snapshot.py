"""Ad-hoc: снимок новостей ДО открытия торгов заданного дня.

Пример:
    py -m scripts.preopen_snapshot 2026-08-05
Записывает output/preopen_YYYY-MM-DD.md со всеми новостями, опубликованными
между окончанием прошлой сессии (18:45 МСК предыдущего торгового дня)
и открытием указанного дня (10:00 МСК).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# позволяем запустить из корня проекта
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance.config import MSK, OUTPUT_DIR
from finance.context import format_snapshot, snapshot as ctx_snapshot
from finance.export import SOURCE_LABELS
from finance.prognosis import factor_changes_backtest, factor_changes_live, format_ranked, rank_expected_moves
from finance.storage import connect

import asyncio


def build(day_str: str) -> str:
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    # ищем предыдущий будний день (грубо: за 4 дня, чтобы после пятницы взять пт вечером)
    prev = day - timedelta(days=1)  # MOEX работает и в выходные
    since = datetime.combine(prev, datetime.min.time(), MSK).replace(hour=18, minute=45)
    until = datetime.combine(day, datetime.min.time(), MSK).replace(hour=10, minute=0)
    # БД хранит published_at в ISO с UTC-суффиксом (+00:00) — сравниваем в UTC
    since_iso = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    until_iso = until.astimezone(timezone.utc).isoformat(timespec="seconds")

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM news WHERE published_at >= ? AND published_at < ? "
            "ORDER BY published_at",
            (since_iso, until_iso),
        ).fetchall()

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    macro: list[dict] = []
    for r in rows:
        try:
            tickers = json.loads(r["tickers"] or "[]")
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tickers, tags = [], []
        item = {
            "source": r["source"],
            "published_at": r["published_at"],
            "title": r["title"],
            "body": (r["body"] or "").strip(),
            "url": r["url"],
            "tickers": tickers,
            "tags": tags,
        }
        if tickers:
            for t in tickers:
                by_ticker[t].append(item)
        else:
            macro.append(item)

    def msk(iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(MSK).strftime("%d.%m %H:%M")

    out: list[str] = []
    out.append(f"# Пре-опен снимок: {day.strftime('%d.%m.%Y')} ({['пн','вт','ср','чт','пт','сб','вс'][day.weekday()]})")
    out.append(f"Окно: {since.strftime('%d.%m %H:%M')} МСК → {until.strftime('%d.%m %H:%M')} МСК")
    out.append(f"Новостей: **{len(rows)}**, с тикером: **{sum(1 for r in rows if json.loads(r['tickers'] or '[]'))}**\n")

    out.append("## По тикерам\n")
    for t in sorted(by_ticker):
        items = by_ticker[t]
        out.append(f"### {t}  ({len(items)})")
        for it in items:
            tags_s = f"  _[{', '.join(it['tags'])}]_" if it["tags"] else ""
            src = SOURCE_LABELS.get(it["source"], it["source"])
            out.append(f"- **[{msk(it['published_at'])} · {src}]** {it['title']}{tags_s}")
            if it["url"]:
                out.append(f"  {it['url']}")
            if it["body"] and len(it["body"]) > 20:
                out.append(f"  _{it['body'][:500]}_")
        out.append("")

    out.append("## Макро / без явной привязки к тикеру\n")
    for it in macro:
        tags_s = f"  _[{', '.join(it['tags'])}]_" if it["tags"] else ""
        src = SOURCE_LABELS.get(it["source"], it["source"])
        out.append(f"- **[{msk(it['published_at'])} · {src}]** {it['title']}{tags_s}")
        if it["url"]:
            out.append(f"  {it['url']}")

    return "\n".join(out)


async def append_context_and_prognosis(day: date, mode: str) -> str:
    """Собирает блок 'Внешний фон' + ожидаемые гэпы. mode: 'live' | 'backtest'."""
    snap = await ctx_snapshot(as_of=day)
    parts = ["\n\n## Внешний фон\n", format_snapshot(snap)]
    if mode == "backtest":
        fc = await factor_changes_backtest(day)
    else:
        fc = await factor_changes_live()
    ranks = rank_expected_moves(fc)
    parts.append("\n\n" + format_ranked(ranks, fc, top=25))
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--no-context", action="store_true", help="Не тянуть внешний фон и модельный прогноз")
    ap.add_argument("--live", action="store_true", help="Live-режим (для утреннего запуска). По умолчанию backtest.")
    args = ap.parse_args()
    md = build(args.date)
    if not args.no_context:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
        mode = "live" if args.live else "backtest"
        extra = asyncio.run(append_context_and_prognosis(day, mode))
        md += extra
    path = OUTPUT_DIR / f"preopen_{args.date}.md"
    path.write_text(md, encoding="utf-8")
    print(f"Written: {path}")


if __name__ == "__main__":
    main()
