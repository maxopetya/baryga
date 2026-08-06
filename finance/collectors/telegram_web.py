"""Коллектор публичных Telegram-каналов через t.me/s/{channel}.

Без Telethon и креденшлов — просто HTML preview, доступный без авторизации.
Даёт до 20 последних сообщений с каждого канала.

Ограничения:
- Только публичные каналы
- Только 20 последних сообщений (пагинация есть, но пока не нужна)
- Тексты приходят «схлопнутыми» — эмодзи-разметка, картинки не тянем
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from selectolax.parser import HTMLParser

from ..http import client
from ..matcher import match
from ..rules import tags

log = logging.getLogger(__name__)

# Whitelist каналов — только те, где регулярно упоминаются тикеры и события
TG_CHANNELS: list[str] = [
    "markettwits",              # MarketTwits — оперативные ленты, много тикеров
    "headlines_for_traders",    # Заголовки для трейдеров
    "bitkogan",                 # Bitkogan — аналитика с упоминанием бумаг
    "cbonds",                   # Cbonds — облигационный рынок, дефолты
    "russianmacro",             # Russian Macro — макро и ставка
]

TG_URL = "https://t.me/s/{channel}"


def _parse_channel(html: str, channel: str, since_utc: datetime) -> list[dict]:
    doc = HTMLParser(html)
    items: list[dict] = []
    for m in doc.css("div.tgme_widget_message"):
        text_node = m.css_first("div.tgme_widget_message_text")
        time_node = m.css_first("time")
        link_node = m.css_first("a.tgme_widget_message_date")
        if not (text_node and time_node):
            continue
        dt_iso = time_node.attributes.get("datetime")
        if not dt_iso:
            continue
        try:
            dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < since_utc:
            continue
        text = text_node.text(separator=" ", strip=True)
        if not text or len(text) < 20:
            continue
        link = (link_node.attributes.get("href") if link_node else None) or f"https://t.me/{channel}"
        # первая строка как заголовок; если весь текст короткий — он же заголовок
        first_line = text.split("\n")[0].split(". ")[0][:200]
        items.append(
            {
                "source": f"tg_{channel}",
                "source_id": link,
                "url": link,
                "published_at": dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "title": first_line,
                "body": text[:2000],
                "tickers": match(text),
                "tags": tags(text),
            }
        )
    return items


async def collect_telegram_web(since_dt: datetime,
                                 channels: list[str] | None = None) -> dict[str, list[dict]]:
    channels = channels or TG_CHANNELS
    since_utc = since_dt.astimezone(timezone.utc)
    result: dict[str, list[dict]] = {}

    async def one(channel: str) -> tuple[str, list[dict]]:
        try:
            async with client() as c:
                r = await c.get(TG_URL.format(channel=channel))
                if r.status_code != 200:
                    return channel, []
                items = _parse_channel(r.text, channel, since_utc)
        except Exception as e:
            log.warning("TG %s failed: %s", channel, e)
            return channel, []
        return channel, items

    results = await asyncio.gather(*(one(ch) for ch in channels))
    for channel, items in results:
        result[f"tg_{channel}"] = items
    return result
