"""Post-market оценка утреннего брифинга.

После закрытия MOEX (~18:50 МСК) для каждого прогноза считаем:
  - actual_day_pct = close(D) / close(D-1) - 1
  - direction_correct = знак(predicted) == знак(actual)
  - magnitude_error_pp = |predicted - actual|
  - attribution — ищем новость, объясняющую промах (для больших ошибок)

Данные хранятся в briefing_evaluations и используются:
  - для вечернего отчёта в Telegram (что угадали, что нет и почему)
  - для еженедельной калибровки news_alpha / confidence-порогов
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from .config import MSK, OUTPUT_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .http import client
from .matcher import match as match_tickers
from .rules import _COMPILED as RULE_PATTERNS
from .storage import connect, init_db
from .telegram_send import send_text

log = logging.getLogger("evaluate")

TICKER_HIST_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR"
    "/securities/{secid}.json"
)
CANDLES_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
    "/securities/{secid}/candles.json"
)

# для attribution: те же строгие теги, что генерят news_alpha
STRONG_TAGS = {"bond_default", "sanctions", "sanctions_lift", "special_div",
               "dividends", "buyback", "redomicil"}


async def _fetch_returns_via_candles(secid: str, day: date, sem) -> dict | None:
    """Тянет candles на 4 дня назад, вытаскивает open/close дня D и prev close.
    Использует candles endpoint, т.к. history часто отстаёт на T+1.
    """
    since = day - timedelta(days=5)
    async with sem, client() as c:
        try:
            r = await c.get(
                CANDLES_URL.format(secid=secid),
                params={
                    "from": since.isoformat(), "till": day.isoformat(),
                    "interval": "60",  # часовые бары, достаточно
                    "iss.meta": "off", "iss.only": "candles",
                },
            )
        except Exception as e:
            log.warning("candles %s: %s", secid, e)
            return None
    if r.status_code != 200:
        return None
    data = r.json().get("candles", {}).get("data", []) or []
    day_s = day.isoformat()
    today_bars = [row for row in data if row[6].startswith(day_s)]
    prior_bars = [row for row in data if row[6] < day_s]
    if not today_bars or not prior_bars:
        return None
    # open — первый бар дня, close — последний бар дня
    open_ = float(today_bars[0][0])
    close_ = float(today_bars[-1][1])
    # prev close — последний бар предыдущего торгового дня
    last_prior_day = prior_bars[-1][6][:10]
    prev_day_bars = [b for b in prior_bars if b[6].startswith(last_prior_day)]
    prev_close = float(prev_day_bars[-1][1]) if prev_day_bars else None
    if not prev_close or prev_close <= 0:
        return None
    return {
        "open_pct": (open_ - prev_close) / prev_close * 100.0,
        "close_pct": (close_ - prev_close) / prev_close * 100.0,
        "prev_close": prev_close,
    }


async def _fetch_actual_returns(secids: list[str], day: date) -> dict[str, dict]:
    """{ticker: {open_pct, close_pct, prev_close}} через candles endpoint."""
    sem = asyncio.Semaphore(6)
    results = await asyncio.gather(*(_fetch_returns_via_candles(t, day, sem) for t in secids))
    return {t: r for t, r in zip(secids, results) if r}


def _find_attribution_for_ticker(secid: str, day: date, since_hour_msk: int = 10) -> dict | None:
    """Ищем новость про тикер, опубликованную после открытия рынка (10:00 МСК).
    Приоритет — со «сильным» тегом; иначе просто первая нетехническая новость.
    """
    since = datetime.combine(day, datetime.min.time(), MSK).replace(hour=since_hour_msk)
    # ищем до полуночи (события пришли за день)
    until = datetime.combine(day + timedelta(days=1), datetime.min.time(), MSK)
    since_iso = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    until_iso = until.astimezone(timezone.utc).isoformat(timespec="seconds")

    tag_rx = {tag: rx for tag, rx, _ in RULE_PATTERNS}
    with connect() as conn:
        rows = conn.execute(
            "SELECT title, url, tickers, tags FROM news "
            "WHERE published_at >= ? AND published_at < ? ORDER BY published_at",
            (since_iso, until_iso),
        ).fetchall()
    # приоритетно — со strong тегом в заголовке, тикер в заголовке
    fallback: dict | None = None
    for r in rows:
        try:
            row_tags = set(json.loads(r["tags"] or "[]"))
        except json.JSONDecodeError:
            continue
        title = r["title"] or ""
        # Cross-contamination fix: тикер должен упоминаться в самом заголовке,
        # а не только в теле. Иначе Smart-Lab пост про MTS может приписаться SOFL.
        title_tickers = set(match_tickers(title))
        if secid not in title_tickers:
            continue
        tech_markers = ("Об изменении", "О порядке", "Дополнительные условия",
                         "О регистрации", "дискретный аукцион", "изменены значения")
        if any(m in title for m in tech_markers):
            continue
        strong_confirmed = [
            t for t in row_tags & STRONG_TAGS
            if tag_rx.get(t) and tag_rx[t].search(title)
        ]
        if strong_confirmed:
            return {"url": r["url"] or "", "title": title, "tags": strong_confirmed}
        if fallback is None:
            fallback = {"url": r["url"] or "", "title": title, "tags": []}
    return fallback


async def evaluate_day(day: date) -> list[dict]:
    """Оценить брифинг за день D. Возвращает список записей с деталями."""
    init_db()
    with connect() as conn:
        preds = conn.execute(
            "SELECT * FROM briefing_predictions WHERE day = ?",
            (day.isoformat(),),
        ).fetchall()
    if not preds:
        log.warning("Нет прогнозов на %s — оценивать нечего.", day)
        return []

    secids = list({p["secid"] for p in preds})
    log.info("Тяну закрытия для %d тикеров...", len(secids))
    actuals = await _fetch_actual_returns(secids, day)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: list[dict] = []
    with connect() as conn:
        for p in preds:
            act = actuals.get(p["secid"])
            if not act or act["close_pct"] is None:
                continue
            predicted = p["effective_pct"] or 0.0
            actual = act["close_pct"]
            dir_ok = 1 if (predicted * actual) > 0 else 0
            mag_err = abs(predicted - actual)
            # attribution — только если промах серьёзный
            attribution = None
            if not dir_ok or mag_err > 2.0:
                attribution = _find_attribution_for_ticker(p["secid"], day)
            att_url = attribution["url"] if attribution else None
            att_title = attribution["title"] if attribution else None
            conn.execute(
                """INSERT INTO briefing_evaluations
                    (day, secid, section, predicted_pct, actual_day_pct,
                     open_close_pct, direction_correct, magnitude_error_pp,
                     attribution_url, attribution_title, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, secid, section) DO UPDATE SET
                     predicted_pct=excluded.predicted_pct,
                     actual_day_pct=excluded.actual_day_pct,
                     open_close_pct=excluded.open_close_pct,
                     direction_correct=excluded.direction_correct,
                     magnitude_error_pp=excluded.magnitude_error_pp,
                     attribution_url=excluded.attribution_url,
                     attribution_title=excluded.attribution_title,
                     created_at=excluded.created_at""",
                (p["day"], p["secid"], p["section"], predicted, actual,
                 act["open_pct"], dir_ok, mag_err, att_url, att_title, now_iso),
            )
            results.append({
                "secid": p["secid"], "section": p["section"], "predicted": predicted,
                "actual": actual, "open_gap": act["open_pct"],
                "dir_ok": dir_ok, "mag_err": mag_err,
                "confidence": p["confidence"], "status": p["status"],
                "attribution": attribution,
            })
        conn.commit()
    return results


def build_evening_report(day: date, results: list[dict]) -> str:
    if not results:
        return f"<b>Оценка {day.strftime('%d.%m')}</b>\n\nНет прогнозов для оценки."

    n = len(results)
    hits = sum(r["dir_ok"] for r in results)
    hit_rate = hits / n * 100
    mae = sum(r["mag_err"] for r in results) / n

    # hit-rate по confidence
    by_conf: dict[str, list[dict]] = {}
    for r in results:
        by_conf.setdefault(r["confidence"] or "?", []).append(r)

    weekday = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][day.weekday()]
    lines: list[str] = []
    lines.append(f"<b>🎯 Разбор {day.strftime('%d.%m')} {weekday}</b>")
    lines.append("")
    lines.append(f"Направление: <b>{hits}/{n} = {hit_rate:.0f}%</b> · Средняя ошибка амплитуды: {mae:.1f} п.п.")
    lines.append("")
    lines.append("<b>По уверенности</b>")
    for conf in ("высокая", "средняя", "низкая"):
        items = by_conf.get(conf, [])
        if not items:
            continue
        h = sum(x["dir_ok"] for x in items)
        lines.append(f"  {conf:8s}  {h}/{len(items)} = {h/len(items)*100:.0f}%")
    lines.append("")

    # Топ попаданий (правильное направление, малая ошибка амплитуды)
    good = [r for r in results if r["dir_ok"] and r["mag_err"] < 2.0]
    good.sort(key=lambda r: abs(r["actual"]), reverse=True)  # самые крупные из попаданий
    if good:
        lines.append("<b>✅ Лучшие попадания</b>")
        for r in good[:5]:
            lines.append(f"• {r['secid']}  прог {r['predicted']:+.1f} → факт {r['actual']:+.1f}  ({r['status']})")
        lines.append("")

    # Топ промахов
    bad = [r for r in results if not r["dir_ok"] or r["mag_err"] > 3.0]
    bad.sort(key=lambda r: r["mag_err"], reverse=True)
    if bad:
        lines.append("<b>❌ Крупные промахи</b>")
        for r in bad[:6]:
            base = f"• {r['secid']}  прог {r['predicted']:+.1f} → факт {r['actual']:+.1f}  ({r['confidence']})"
            att = r.get("attribution")
            if att and att.get("title"):
                src = "источник"
                url = att["url"]
                if url.startswith("http"):
                    domain = url.split("/")[2]
                    src_map = {
                        "www.kommersant.ru": "Ъ", "www.rbc.ru": "РБК",
                        "www.vedomosti.ru": "Вед", "www.interfax.ru": "И-факс",
                        "1prime.ru": "Прайм", "www.moex.com": "MOEX",
                        "smart-lab.ru": "smart-lab", "ru.investing.com": "Investing",
                    }
                    src = src_map.get(domain, domain)
                    if domain.startswith("t.me"):
                        src = "TG"
                lines.append(base)
                lines.append(f"    причина: {att['title'][:100]} <a href=\"{url}\">{src}</a>")
            else:
                lines.append(f"{base}\n    причина: <i>не нашли новости, стоит проверить руками</i>")
        lines.append("")

    lines.append("<i>Разбор идёт в БД для еженедельной калибровки news_alpha и порогов confidence.</i>")
    return "\n".join(lines)


async def run_and_send(day: date, dry_run: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    results = await evaluate_day(day)
    report = build_evening_report(day, results)
    out = OUTPUT_DIR / f"evening_{day.isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(f"Written: {out}")
    if dry_run:
        print("Dry-run: не шлём в Telegram.")
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Токен/chat_id не заданы — пропускаем отправку.")
        return
    await send_text(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, report)
    print("Отправлено в Telegram.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (по умолчанию сегодня МСК)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    day = date.fromisoformat(args.date) if args.date else datetime.now(MSK).date()
    asyncio.run(run_and_send(day, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
