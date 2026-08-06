"""Экспорт свежих новостей в Markdown-сводку для скринера."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import MSK, NEWS_WINDOW_HOURS, OUTPUT_DIR
from .storage import fetch_news_since

SOURCE_LABELS = {
    "rbc":          "РБК",
    "vedomosti":    "Ведомости",
    "interfax":     "Интерфакс",
    "kommersant_b": "Коммерсантъ Бизнес",
    "kommersant_f": "Коммерсантъ Финансы",
    "prime":        "Прайм",
    "moex":         "MOEX",
    "edisclosure":  "e-disclosure",
}


def _msk(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.astimezone(MSK).strftime("%d.%m %H:%M МСК")


def build_markdown(hours: int) -> str:
    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_iso = since_dt.isoformat(timespec="seconds")
    rows = fetch_news_since(since_iso)

    # Разделяем: с тикерами (важные) vs без (общий фон)
    with_ticker = []
    macro = []
    for r in rows:
        try:
            tickers = json.loads(r["tickers"] or "[]")
        except json.JSONDecodeError:
            tickers = []
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        item = {
            "source": r["source"],
            "published_at": r["published_at"],
            "title": r["title"],
            "body": (r["body"] or "").strip(),
            "url": r["url"],
            "tickers": tickers,
            "tags": tags,
        }
        (with_ticker if tickers else macro).append(item)

    # По тикеру → новости, отсортированные по времени
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for it in with_ticker:
        for t in it["tickers"]:
            by_ticker[t].append(it)

    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")
    out: list[str] = []
    out.append(f"# Daily News — {now_msk}")
    out.append(f"Окно: последние {hours} ч. Источники: RSS деловых СМИ + MOEX + e-disclosure.\n")
    out.append(f"Всего новостей: **{len(rows)}**, из них с привязкой к тикеру: **{len(with_ticker)}**\n")

    # ── Новости по тикерам
    out.append("## По тикерам\n")
    if not by_ticker:
        out.append("_Нет новостей с явной привязкой к тикеру в окне._\n")
    for t in sorted(by_ticker.keys()):
        items = sorted(by_ticker[t], key=lambda x: x["published_at"], reverse=True)
        out.append(f"### {t}  ({len(items)} новост{'ь' if len(items) == 1 else 'и'})\n")
        for it in items:
            tags_str = ", ".join(it["tags"]) if it["tags"] else ""
            src_label = SOURCE_LABELS.get(it["source"], it["source"])
            time_str = _msk(it["published_at"])
            head = f"- **[{time_str} · {src_label}]** {it['title']}"
            if tags_str:
                head += f"  \n  _теги:_ {tags_str}"
            if it["url"]:
                head += f"  \n  {it['url']}"
            if it["body"] and len(it["body"]) > 20:
                head += f"  \n  _{it['body'][:400]}_"
            out.append(head)
        out.append("")

    # ── Макро/общий фон
    out.append("## Макро / без явной привязки к тикеру\n")
    if not macro:
        out.append("_Пусто._\n")
    for it in sorted(macro, key=lambda x: x["published_at"], reverse=True)[:80]:
        tags_str = ", ".join(it["tags"]) if it["tags"] else ""
        src_label = SOURCE_LABELS.get(it["source"], it["source"])
        head = f"- **[{_msk(it['published_at'])} · {src_label}]** {it['title']}"
        if tags_str:
            head += f"  \n  _теги:_ {tags_str}"
        if it["url"]:
            head += f"  \n  {it['url']}"
        out.append(head)

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=NEWS_WINDOW_HOURS)
    ap.add_argument("--out", type=str, default=None, help="Путь для .md (по умолчанию output/daily_news_YYYY-MM-DD.md)")
    args = ap.parse_args()

    md = build_markdown(args.hours)
    fname = args.out or f"daily_news_{datetime.now(MSK).strftime('%Y-%m-%d')}.md"
    path = OUTPUT_DIR / fname
    path.write_text(md, encoding="utf-8")
    print(f"Written: {path}")


if __name__ == "__main__":
    main()
