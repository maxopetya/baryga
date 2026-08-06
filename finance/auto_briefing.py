"""Автоматический генератор утреннего брифинга — без участия LLM.

Строгий шаблон по правилам из screener_prompt.md:
  - Настроение из предсказанного IMOEX
  - Топ-15 вверх (≤4 на сектор), уверенность по формуле |exp|/vol
  - Топ вниз, если модель в целом медвежья
  - «Отдельно» — сильные новости pre-open с тикером и хардовым событием

Использование:
    python -m finance.auto_briefing              # шлёт в Telegram (нужны env vars)
    python -m finance.auto_briefing --dry-run    # только записывает файл
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import MSK, OUTPUT_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .prognosis import factor_changes_overnight, overnight_predictor_changes, rank_expected_moves
from .rules import _COMPILED as RULE_PATTERNS
from .storage import connect
from .telegram_send import send_text

log = logging.getLogger("auto_briefing")

# ── Именование секторов (для колонки «драйвер») ──
SECTOR_LABEL = {
    "MOEXOG": "нефтегаз",
    "MOEXMM": "металлы",
    "MOEXFN": "финансы",
    "MOEXCN": "потреб",
    "MOEXIT": "IT",
    "MOEXTLC": "телеком",
    "MOEXTN": "транспорт",
    "MOEXEU": "энергетика",
    "MOEXRE": "недвижимость",
    "MOEXCH": "химия",
}

GOLD_TICKERS = {"PLZL", "SELG", "UGLD"}  # для лейбла «золото» вместо «металлы»

# ── Короткие имена компаний для отображения ──
SHORT_NAMES = {
    "SBER": "Сбер", "SBERP": "Сбер преф",
    "GAZP": "Газпром",
    "LKOH": "Лукойл",
    "GMKN": "Норникель",
    "ROSN": "Роснефть",
    "NVTK": "Новатэк",
    "TATN": "Татнефть", "TATNP": "Татнефть преф",
    "SNGS": "Сургут", "SNGSP": "Сургут преф",
    "MGNT": "Магнит",
    "MTSS": "МТС",
    "YDEX": "Яндекс",
    "VTBR": "ВТБ",
    "MOEX": "Мосбиржа",
    "PLZL": "Полюс",
    "CHMF": "Северсталь",
    "NLMK": "НЛМК",
    "MAGN": "ММК",
    "ALRS": "Алроса",
    "PHOR": "ФосАгро",
    "TRNFP": "Транснефть",
    "RTKM": "Ростелеком",
    "FEES": "Россети",
    "IRAO": "Интер РАО",
    "HYDR": "РусГидро",
    "AFLT": "Аэрофлот",
    "AFKS": "АФК Система",
    "OZON": "Ozon",
    "TCSG": "Т-Банк", "T": "Т-Банк",
    "VKCO": "ВК",
    "POSI": "Позитив",
    "HHRU": "HeadHunter",
    "FIVE": "X5", "X5": "X5",
    "FIXP": "Fix Price",
    "SMLT": "Самолёт",
    "PIKK": "ПИК",
    "LSRG": "ЛСР",
    "ETLN": "Эталон",
    "RUAL": "Русал",
    "MTLR": "Мечел", "MTLRP": "Мечел преф",
    "ENPG": "Эн+",
    "SGZH": "Сегежа",
    "AKRN": "Акрон",
    "KZOS": "Казаньоргсинтез",
    "NKNC": "НКНХ",
    "UPRO": "Юнипро",
    "MSNG": "Мосэнерго",
    "OGKB": "ОГК-2",
    "TGKA": "ТГК-1",
    "BSPB": "Банк СПб",
    "CBOM": "МКБ",
    "SVCB": "Совкомбанк",
    "RENI": "Ренессанс",
    "SFIN": "SFI",
    "GEMC": "ЮМГ",
    "MDMG": "Мать и дитя",
    "RAGR": "Русагро", "AGRO": "Русагро",
    "BELU": "НоваБев",
    "ABRD": "Абрау",
    "BANE": "Башнефть", "BANEP": "Башнефть преф",
    "SELG": "Селигдар",
    "UGLD": "ЮГК",
    "LENT": "Лента",
    "MVID": "М.видео",
    "DIAS": "Диасофт",
    "ASTR": "Астра",
    "SOFL": "Софтлайн",
    "WUSH": "Whoosh",
    "DELI": "Делимобиль",
    "ABIO": "Артген",
    "IVAT": "Ива",
    "EUTR": "ЕвроТранс",
    "RNFT": "РуссНефть",
    "FESH": "ДВМП",
    "DATA": "Аренадата",
    "MRKC": "Россети Центр",
    "RASP": "Распадская",
    "TRMK": "ТМК",
}

# ── Сильные новостные теги, дающие «высокую» уверенность в направлении ──
STRONG_BULLISH_TAGS = {"dividends", "special_div", "buyback", "sanctions_lift", "redomicil"}
STRONG_BEARISH_TAGS = {"bond_default", "sanctions"}


def _short(secid: str) -> str:
    """Короткое имя компании: словарь или очистка secname."""
    if secid in SHORT_NAMES:
        return SHORT_NAMES[secid]
    with connect() as conn:
        r = conn.execute("SELECT secname FROM tickers WHERE secid=?", (secid,)).fetchone()
    if not r:
        return secid
    name = r["secname"]
    for bad in ("ПАО", "ОАО", "АО", "МКПАО", "НК", "\"", "'", "  "):
        name = name.replace(bad, " ")
    name = " ".join(name.split()).strip()
    if " " in name and len(name) > 22:
        name = name.split()[0]
    return name


def _driver_label(ticker: dict, sector: str | None) -> str:
    """Определяет короткий 'драйвер' по contributions."""
    contribs = {c["factor"]: c["contribution_pct"] for c in ticker["contributions"]}
    gold = contribs.get("GOLD", 0)
    br = contribs.get("BR", 0)
    imoex = contribs.get("IMOEX", 0)
    if ticker["secid"] in GOLD_TICKERS and abs(gold) >= 0.05:
        return "золото"
    if sector and sector in SECTOR_LABEL:
        # если и IMOEX и sector в одну сторону, но сектор сильнее — sector label
        return SECTOR_LABEL[sector]
    return "общерыночный"


def _confidence(exp_pct: float, vol_pct: float) -> str:
    if vol_pct <= 0:
        return "низкая"
    ratio = abs(exp_pct) / vol_pct
    if ratio >= 0.7:
        return "высокая"
    if ratio >= 0.4:
        return "средняя"
    return "низкая"


def _mood(imoex_pct: float | None, overnight_bright: bool) -> str:
    if imoex_pct is None:
        return "нейтральное"
    if imoex_pct >= 0.3:
        return "позитивное"
    if imoex_pct <= -0.3:
        return "негативное" + (" (но overnight-фон бычий)" if overnight_bright else "")
    return "нейтральное"


def _diversified_pick(ranked: list, sector_of, max_per_sector: int, limit: int, positive_only: bool):
    per_sec: dict[str, int] = {}
    picked = []
    for r in ranked:
        exp = r["expected_open_pct"]
        if positive_only and exp <= 0:
            break
        if not positive_only and exp >= 0:
            break
        sec = sector_of(r["secid"]) or "?"
        if per_sec.get(sec, 0) >= max_per_sector:
            continue
        picked.append(r)
        per_sec[sec] = per_sec.get(sec, 0) + 1
        if len(picked) >= limit:
            break
    return picked


def _fetch_strong_news(day: date) -> list[dict]:
    """Ищем pre-open новости с сильным тегом И привязкой к конкретному тикеру."""
    # окно: 18:45 МСК предыдущего дня → 10:00 МСК day
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    since = datetime.combine(prev, datetime.min.time(), MSK).replace(hour=18, minute=45)
    until = datetime.combine(day, datetime.min.time(), MSK).replace(hour=10, minute=0)
    since_iso = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    until_iso = until.astimezone(timezone.utc).isoformat(timespec="seconds")

    with connect() as conn:
        rows = conn.execute(
            "SELECT title, url, tickers, tags FROM news "
            "WHERE published_at >= ? AND published_at < ? ORDER BY published_at DESC",
            (since_iso, until_iso),
        ).fetchall()

    result: list[dict] = []
    seen_tickers: set[str] = set()
    # регекс-таблица для валидации тега на уровне ЗАГОЛОВКА
    tag_rx = {tag: rx for tag, rx, _ in RULE_PATTERNS}

    for r in rows:
        try:
            tickers = json.loads(r["tickers"] or "[]")
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            continue
        strong = set(tags) & (STRONG_BULLISH_TAGS | STRONG_BEARISH_TAGS)
        if not strong or not tickers:
            continue
        # антифолсу: тег должен подтверждаться заголовком, а не только телом
        confirmed = {t for t in strong if tag_rx.get(t) and tag_rx[t].search(r["title"] or "")}
        if not confirmed:
            continue
        primary = tickers[0]
        if primary in seen_tickers:
            continue
        seen_tickers.add(primary)
        bearish = bool(confirmed & STRONG_BEARISH_TAGS)
        result.append({
            "ticker": primary,
            "title": r["title"],
            "url": r["url"],
            "tags": list(confirmed),
            "bearish": bearish,
        })
        if len(result) >= 3:
            break
    return result


def _fmt_row(name: str, ticker: str, pct: float, driver: str, conf: str,
             name_w: int = 15, tick_w: int = 8) -> str:
    return f"{name:<{name_w}}{('(' + ticker + ')'):<{tick_w}}{pct:+.1f}%  — {driver} ({conf})"


async def build_briefing(day: date | None = None) -> str:
    day = day or datetime.now(MSK).date()

    fc, det = await factor_changes_overnight(day)
    overn = det["overnight_inputs_pct"]
    preds = det["predictions"]
    imoex_pct = preds.get("IMOEX", {}).get("expected_pct")
    # overnight-фон «бычий», если Nikkei или SPX или BRENT_LIVE или GOLD_LIVE > 1%
    overnight_bright = any(overn.get(k, 0) > 1.0 for k in ("SPX", "NIKKEI", "GOLD_LIVE", "BRENT_LIVE"))

    ranks = rank_expected_moves(fc)

    with connect() as conn:
        def sector_of(t):
            r = conn.execute("SELECT sector_index, vol_daily FROM ticker_vol WHERE secid=?", (t,)).fetchone()
            return r["sector_index"] if r else None
        def vol_of(t):
            r = conn.execute("SELECT vol_daily FROM ticker_vol WHERE secid=?", (t,)).fetchone()
            return (r["vol_daily"] or 0) * 100 if r else 0

        ups = _diversified_pick(ranks, sector_of, max_per_sector=4, limit=15, positive_only=True)
        downs_all = sorted(ranks, key=lambda x: x["expected_open_pct"])  # самые отрицательные вперёд
        downs = _diversified_pick(downs_all, sector_of, max_per_sector=3, limit=6, positive_only=False)

    strong_news = _fetch_strong_news(day)

    weekday = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][day.weekday()]
    date_h = day.strftime("%d.%m")
    lines: list[str] = []
    lines.append(f"<b>📊 {date_h} {weekday} · MOEX через ~30 мин</b>")
    lines.append("")

    mood = _mood(imoex_pct, overnight_bright)
    # шапка overnight
    sp = overn.get("SPX", 0); ni = overn.get("NIKKEI", 0)
    br = overn.get("BRENT_LIVE", 0); gd = overn.get("GOLD_LIVE", 0)
    lines.append(f"Настроение {mood} · США {sp:+.1f}% · золото {gd:+.1f}% · нефть {br:+.1f}% · Nikkei {ni:+.1f}%")
    lines.append("")

    # Топ вверх
    if ups:
        lines.append(f"<b>Топ-{len(ups)} вверх (≤4 на сектор)</b>")
        rows = []
        for r in ups:
            t = r["secid"]
            exp = r["expected_open_pct"]
            v = vol_of(t)
            conf = _confidence(exp, v)
            driver = _driver_label(r, sector_of(t))
            rows.append(_fmt_row(_short(t), t, exp, driver, conf))
        lines.append("<pre>" + "\n".join(rows) + "</pre>")
    else:
        lines.append("<b>Топ вверх</b>")
        lines.append("Модель бычьих кандидатов не даёт. Overnight-фон:"
                     f" Nikkei {ni:+.1f}%, золото {gd:+.1f}% — реальность может разойтись с моделью.")
    lines.append("")

    # Топ вниз — показываем только если IMOEX прогноз явно отрицательный
    if imoex_pct is not None and imoex_pct <= -0.15 and downs:
        lines.append("<b>Топ вниз (модель)</b>")
        rows = []
        for r in downs:
            t = r["secid"]
            exp = r["expected_open_pct"]
            v = vol_of(t)
            conf = _confidence(exp, v)
            driver = _driver_label(r, sector_of(t))
            rows.append(_fmt_row(_short(t), t, exp, driver, conf))
        lines.append("<pre>" + "\n".join(rows) + "</pre>")
        lines.append("")

    # Отдельно — новости
    if strong_news:
        lines.append("<b>Отдельно</b>")
        for n in strong_news:
            arrow = "🔻" if n["bearish"] else "🟢"
            short = _short(n["ticker"])
            title = n["title"][:180]
            url = n["url"] or ""
            src = url.split("/")[2] if url.startswith("http") else "источник"
            src_map = {
                "www.kommersant.ru": "Коммерсант",
                "www.rbc.ru": "РБК",
                "www.vedomosti.ru": "Ведомости",
                "www.interfax.ru": "Интерфакс",
                "1prime.ru": "Прайм",
                "www.moex.com": "MOEX",
            }
            src_name = src_map.get(src, src)
            lines.append(f"{arrow} <b>{short} ({n['ticker']})</b>: {title} <a href=\"{url}\">{src_name}</a>")
        lines.append("")

    lines.append("Уверенность — про направление, не про амплитуду. % — базовое ожидание модели.")
    lines.append("Не является инвестиционной рекомендацией.")

    return "\n".join(lines)


async def main_async(dry_run: bool, out_dir: Path) -> None:
    day = datetime.now(MSK).date()
    text = await build_briefing(day)
    out_dir.mkdir(exist_ok=True)
    fname = out_dir / f"daily_briefing_{day.isoformat()}.md"
    fname.write_text(text, encoding="utf-8")
    print(f"Written: {fname}")
    if dry_run:
        print("Dry-run: пропускаем отправку.")
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID пусты — не отправляю.")
        return
    await send_text(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)
    print("Отправлено в Telegram.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Не отправлять в Telegram")
    args = ap.parse_args()
    asyncio.run(main_async(args.dry_run, OUTPUT_DIR))


if __name__ == "__main__":
    main()
