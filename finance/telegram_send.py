"""Отправка Markdown-брифинга в Telegram.

Использование:
    python -m finance.telegram_send output/daily_briefing_2026-08-06.md

Требуется TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env.

Как получить chat_id:
    1. Создайте бота у @BotFather, сохраните token в .env
    2. Напишите боту любое сообщение
    3. Запустите: python -m finance.telegram_send --whoami
       — покажет ваш chat_id, впишите в .env
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

API = "https://api.telegram.org/bot{token}/{method}"
# Telegram лимит на текст в одном сообщении
MAX_MSG = 4000  # запас от 4096


def _chunks(text: str, limit: int = MAX_MSG) -> list[str]:
    """Режем длинный markdown по абзацам, чтобы не рвать посередине заголовка."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        block = para + "\n\n"
        if size + len(block) > limit and buf:
            parts.append("".join(buf).rstrip())
            buf = []
            size = 0
        # если отдельный абзац сам больше лимита — рубим на строки
        if len(block) > limit:
            for line in block.splitlines(keepends=True):
                if size + len(line) > limit and buf:
                    parts.append("".join(buf).rstrip())
                    buf = []
                    size = 0
                buf.append(line)
                size += len(line)
        else:
            buf.append(block)
            size += len(block)
    if buf:
        parts.append("".join(buf).rstrip())
    return parts


async def send_text(token: str, chat_id: str, text: str) -> None:
    """Отправка markdown-текста. Пробуем HTML (надёжнее), в случае ошибки — plain."""
    async with httpx.AsyncClient(timeout=30) as c:
        for part in _chunks(text):
            r = await c.post(
                API.format(token=token, method="sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                r2 = await c.post(
                    API.format(token=token, method="sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": part,
                        "disable_web_page_preview": True,
                    },
                )
                r2.raise_for_status()


async def send_document(token: str, chat_id: str, path: Path, caption: str = "") -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        with path.open("rb") as f:
            r = await c.post(
                API.format(token=token, method="sendDocument"),
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (path.name, f, "text/markdown")},
            )
            r.raise_for_status()


async def get_updates(token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(API.format(token=token, method="getUpdates"))
        r.raise_for_status()
        return r.json().get("result", [])


def _require(name: str, value: str) -> str:
    if not value:
        print(f"ERROR: {name} не задан в .env", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="Путь к .md для отправки")
    ap.add_argument("--as-file", action="store_true", help="Отправить документом, а не текстом")
    ap.add_argument("--whoami", action="store_true", help="Показать chat_id последнего входящего")
    args = ap.parse_args()

    token = _require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)

    if args.whoami:
        updates = asyncio.run(get_updates(token))
        if not updates:
            print("Нет входящих. Напишите боту любое сообщение и повторите.")
            return
        for u in updates[-5:]:
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            print(f"chat_id={chat.get('id')}  type={chat.get('type')}  name={chat.get('first_name') or chat.get('title')}")
        return

    chat_id = _require("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    if not args.path:
        print("Укажите путь к .md или --whoami", file=sys.stderr)
        sys.exit(2)
    path = Path(args.path)
    if not path.exists():
        print(f"Файл не найден: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    if args.as_file:
        asyncio.run(send_document(token, chat_id, path, caption=path.stem))
        print(f"Отправлен файл: {path.name}")
    else:
        asyncio.run(send_text(token, chat_id, text))
        print(f"Отправлено сообщений: {len(_chunks(text))}")


if __name__ == "__main__":
    main()
