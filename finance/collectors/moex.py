"""Коллектор новостей MOEX через ISS."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..http import client
from ..matcher import match
from ..rules import tags

log = logging.getLogger(__name__)

# ISS site news (агрегация уведомлений биржи и раскрытий)
ISS_SITENEWS = (
    "https://iss.moex.com/iss/sitenews.json"
    "?iss.meta=off&iss.only=sitenews&lang=ru&limit=200&start=0"
)


async def collect_moex(since_dt: datetime) -> list[dict]:
    async with client() as c:
        r = await c.get(ISS_SITENEWS)
        r.raise_for_status()
        data = r.json().get("sitenews", {})

    cols = data.get("columns", [])
    if not cols:
        return []
    idx = {name: i for i, name in enumerate(cols)}
    since_iso = since_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    out: list[dict] = []
    for row in data.get("data", []):
        title = row[idx.get("title", 0)] if "title" in idx else ""
        body_html = row[idx.get("body", 0)] if "body" in idx else ""
        published_raw = row[idx.get("published_at", 0)] if "published_at" in idx else None
        news_id = row[idx.get("id", 0)] if "id" in idx else None
        if not (title and published_raw):
            continue
        # published_at в ISS: 'YYYY-MM-DD HH:MM:SS' МСК
        try:
            dt = datetime.strptime(published_raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc  # ISS отдаёт в UTC для sitenews
            )
        except ValueError:
            continue
        published_iso = dt.isoformat(timespec="seconds")
        if published_iso < since_iso:
            continue
        text = f"{title}. {body_html}"
        out.append(
            {
                "source": "moex",
                "source_id": str(news_id) if news_id is not None else None,
                "url": f"https://www.moex.com/n{news_id}" if news_id else None,
                "published_at": published_iso,
                "title": title.strip(),
                "body": body_html,
                "tickers": match(text),
                "tags": tags(text),
            }
        )
    return out
