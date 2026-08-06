"""Коллектор раскрытий с e-disclosure.ru.

Формат страницы lastnews выдаёт HTML со списком раскрытий:
<div class="lastNewsItem">
    <a href="/portal/event.aspx?EventId=..."> ... </a>
    <span>время</span>
    <div>заголовок</div>
    <div class="lastNewsItemInfo">эмитент, тип события</div>
</div>

Реализация основана на публичной странице https://www.e-disclosure.ru/portal/lastnews.aspx
Работает при доступе с российского IP. Из тестовой среды (не РФ) возвращается 403.
Скрипт корректно логирует ошибку и не падает.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from selectolax.parser import HTMLParser

from ..http import client
from ..matcher import match
from ..rules import tags

log = logging.getLogger(__name__)

URL = "https://www.e-disclosure.ru/portal/lastnews.aspx"

_TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})")


async def collect_edisclosure(since_dt: datetime) -> list[dict]:
    async with client() as c:
        r = await c.get(URL)
        if r.status_code == 403:
            log.warning("e-disclosure returned 403 (likely geoblocked). Skipping.")
            return []
        r.raise_for_status()
        html = r.text

    doc = HTMLParser(html)
    out: list[dict] = []
    since_iso = since_dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    for node in doc.css("div.lastNewsItem, tr.lastNewsRow"):
        link_node = node.css_first("a[href*='EventId']") or node.css_first("a")
        if not link_node:
            continue
        href = link_node.attributes.get("href", "")
        if href.startswith("/"):
            url = "https://www.e-disclosure.ru" + href
        else:
            url = href
        text_all = node.text(separator=" ", strip=True)
        m = _TIME_RE.search(text_all)
        if not m:
            continue
        dd, mm, yy, hh, mi = m.groups()
        # e-disclosure — время МСК
        dt = datetime(
            int(yy), int(mm), int(dd), int(hh), int(mi), tzinfo=timezone.utc
        )
        published_iso = dt.isoformat(timespec="seconds")
        if published_iso < since_iso:
            continue
        title = link_node.text(strip=True) or text_all[:200]
        emit_node = node.css_first("div.lastNewsItemInfo")
        info = emit_node.text(separator=" ", strip=True) if emit_node else ""
        blob = f"{title}. {info}"
        out.append(
            {
                "source": "edisclosure",
                "source_id": href,
                "url": url,
                "published_at": published_iso,
                "title": title,
                "body": info,
                "tickers": match(blob),
                "tags": tags(blob),
            }
        )
    return out
