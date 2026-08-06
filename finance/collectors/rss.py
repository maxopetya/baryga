"""RSS-коллектор деловых СМИ."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
from selectolax.parser import HTMLParser

from ..config import USER_AGENT
from ..matcher import match
from ..rules import tags

log = logging.getLogger(__name__)

# name → (feed_url, source_slug)
RSS_SOURCES: dict[str, str] = {
    "rbc":          "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "vedomosti":    "https://www.vedomosti.ru/rss/rubric/finance",
    "interfax":     "https://www.interfax.ru/rss.asp",
    "kommersant_b": "https://www.kommersant.ru/RSS/section-business.xml",
    "kommersant_f": "https://www.kommersant.ru/RSS/section-finance.xml",
    "prime":        "https://1prime.ru/export/rss2/index.xml",
}


def _parse_date(entry) -> str | None:
    for attr in ("published", "updated", "pubDate"):
        raw = entry.get(attr)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if entry.get("published_parsed"):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    return None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        return tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)
    except Exception:
        return html


def _fetch_feed(url: str) -> list[dict]:
    f = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    items: list[dict] = []
    for e in f.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        summary = e.get("summary") or e.get("description") or ""
        body = _strip_html(summary)
        published = _parse_date(e)
        if not published:
            continue
        blob = f"{title}. {body}"
        items.append(
            {
                "source_id": e.get("id") or e.get("guid"),
                "url": e.get("link"),
                "published_at": published,
                "title": title,
                "body": body,
                "tickers": match(blob),
                "tags": tags(blob),
            }
        )
    return items


async def collect_rss(since_dt: datetime, sources: list[str] | None = None) -> dict[str, list[dict]]:
    """Собрать RSS. Возвращает {source_slug: [news_dict, ...]}."""
    picked = sources or list(RSS_SOURCES.keys())
    loop = asyncio.get_running_loop()

    async def one(name: str) -> tuple[str, list[dict]]:
        url = RSS_SOURCES[name]
        try:
            items = await loop.run_in_executor(None, _fetch_feed, url)
        except Exception as e:
            log.warning("RSS %s failed: %s", name, e)
            return name, []
        # добавляем source, фильтруем по окну
        since_iso = since_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        filtered = [
            {**it, "source": name} for it in items if it["published_at"] >= since_iso
        ]
        return name, filtered

    results = await asyncio.gather(*(one(n) for n in picked))
    return dict(results)
