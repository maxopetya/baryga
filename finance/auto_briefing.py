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
from .matcher import match as match_tickers
from .morning import morning_for
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

# News-alpha — сколько % ожидаем на открытии из-за данного типа события.
# Величины — грубые исторические оценки, не претендуют на точность, но задают
# правильный порядок: дефолт двигает бумагу гораздо сильнее, чем объявление дивов.
NEWS_ALPHA_PCT: dict[str, float] = {
    "bond_default":   -12.0,
    "sanctions":       -6.0,
    "sanctions_lift":  +5.0,
    "special_div":     +6.0,
    "dividends":       +3.0,
    "buyback":         +3.0,
    "redomicil":       +2.0,
    "mna":              0.0,   # неопределённое направление
    "earnings":         0.0,   # знак зависит от того, лучше/хуже консенсуса
}


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


def _reinforce_status(overnight_pct: float, morning_pct: float | None, vol_pct: float) -> tuple[str, str]:
    """Возвращает (метка_статуса, уверенность).

    Логика:
      подтв.     — overnight и утро в одну сторону, амплитуды сопоставимы
      усилено    — та же сторона, утро значительно сильнее (в 2× или больше)
      конфликт   — знаки противоположны (доверяем утру, но confidence падает)
      только утро — overnight ≈ 0, утро выделяется
      только модель — утра нет вообще
      нейтр.     — оба слабые
    """
    NOISE = 0.15  # % — ниже считаем «нулём»
    if morning_pct is None:
        # доверяем только overnight
        conf = _confidence(overnight_pct, vol_pct)
        if abs(overnight_pct) < NOISE:
            return "нейтр.", "низкая"
        return "только модель", conf

    o_signal = abs(overnight_pct) >= NOISE
    m_signal = abs(morning_pct) >= NOISE
    if not o_signal and not m_signal:
        return "нейтр.", "низкая"
    if not o_signal:
        return "только утро", _confidence(morning_pct, vol_pct)
    if not m_signal:
        return "только модель", _confidence(overnight_pct, vol_pct)

    same_dir = (overnight_pct * morning_pct) > 0
    ratio_m_to_o = abs(morning_pct) / max(abs(overnight_pct), 0.01)
    if same_dir:
        conf = _confidence(morning_pct, vol_pct)
        # подтверждённый сигнал заслуживает бонус к уверенности
        if conf == "низкая" and abs(morning_pct) / max(vol_pct, 0.01) >= 0.3:
            conf = "средняя"
        elif conf == "средняя" and abs(morning_pct) / max(vol_pct, 0.01) >= 0.6:
            conf = "высокая"
        label = "усилено" if ratio_m_to_o >= 2.0 else "подтв."
        return label, conf
    else:
        # знаки не совпадают: доверяем утру (это уже случившийся факт), но подтверждения нет
        conf = _confidence(morning_pct, vol_pct)
        # снижаем уверенность на одну ступень
        step = {"высокая": "средняя", "средняя": "низкая", "низкая": "низкая"}
        return "конфликт", step[conf]


def _mood(imoex_pct: float | None, overnight_bright: bool) -> str:
    if imoex_pct is None:
        return "нейтральное"
    if imoex_pct >= 0.3:
        return "позитивное"
    if imoex_pct <= -0.3:
        return "негативное" + (" (но overnight-фон бычий)" if overnight_bright else "")
    return "нейтральное"


def _diversified_pick(ranked: list, sector_of, max_per_sector: int, limit: int, positive_only: bool, key: str = "expected_open_pct"):
    per_sec: dict[str, int] = {}
    picked = []
    for r in ranked:
        exp = r[key]
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


MOEX_TECHNICAL_PATTERNS = (
    "Об изменении", "О порядке", "Дополнительные условия", "О регистрации",
    "дискретный аукцион", "изменены значения", "приостановке торгов",
    "О приостановке", "О возобновлении", "заявок на", "уровня листинга",
    "торгового дня", "депозитный аукцион", "фиксингов",
)


def _is_technical_moex(title: str) -> bool:
    if not title:
        return False
    for p in MOEX_TECHNICAL_PATTERNS:
        if p in title:
            return True
    return False


def _find_news_for_tickers(tickers: set[str], day: date) -> dict[str, dict | None]:
    """Ищем упоминания тикеров в новостях за последние 30 ч.
    Возвращаем {ticker: {title, url}} или {ticker: None} если ничего вменяемого не нашли.
    """
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    since = datetime.combine(prev, datetime.min.time(), MSK).replace(hour=6)
    until = datetime.combine(day, datetime.min.time(), MSK).replace(hour=10, minute=0)
    since_iso = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    until_iso = until.astimezone(timezone.utc).isoformat(timespec="seconds")
    result: dict[str, dict] = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT title, url, tickers FROM news WHERE published_at >= ? AND published_at < ? "
            "ORDER BY published_at DESC",
            (since_iso, until_iso),
        ).fetchall()
    for r in rows:
        try:
            row_tickers = set(json.loads(r["tickers"] or "[]"))
        except json.JSONDecodeError:
            continue
        match = tickers & row_tickers
        if not match:
            continue
        if _is_technical_moex(r["title"]):
            continue
        for tk in match:
            if tk in result:
                continue
            result[tk] = {"title": r["title"], "url": r["url"] or ""}
    # Для тикеров без новости оставляем метку None (чтобы в брифинге видеть «нет данных»)
    return {tk: result.get(tk) for tk in tickers}


def _news_alpha_map(day: date) -> dict[str, float]:
    """Event-driven alpha per ticker из pre-open новостей.

    Правила чтобы избежать cross-contamination:
      1. Тег события подтверждён в ЗАГОЛОВКЕ (не только в теле)
      2. Alpha применяется только к тикерам, тоже упомянутым в ЗАГОЛОВКЕ
         (иначе Smart-Lab-статья про DIAS с побочным упоминанием EUTR в теле
          не должна давать EUTR-у alpha от «дивидендов»)
      3. Если по одному тикеру много новостей — берём самый сильный по модулю alpha
    """
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    since = datetime.combine(prev, datetime.min.time(), MSK).replace(hour=18, minute=45)
    until = datetime.combine(day, datetime.min.time(), MSK).replace(hour=10, minute=0)
    since_iso = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    until_iso = until.astimezone(timezone.utc).isoformat(timespec="seconds")

    tag_rx = {tag: rx for tag, rx, _ in RULE_PATTERNS}
    alphas: dict[str, float] = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT title, tickers, tags FROM news "
            "WHERE published_at >= ? AND published_at < ? ORDER BY published_at DESC",
            (since_iso, until_iso),
        ).fetchall()
    for r in rows:
        try:
            row_tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            continue
        if not row_tags:
            continue
        title = r["title"] or ""
        # тег обязан быть в ЗАГОЛОВКЕ, иначе — это шум из тела
        confirmed = [t for t in row_tags if tag_rx.get(t) and tag_rx[t].search(title)]
        tag_alphas = [NEWS_ALPHA_PCT.get(t, 0.0) for t in confirmed]
        tag_alphas = [a for a in tag_alphas if a != 0]
        if not tag_alphas:
            continue
        alpha = max(tag_alphas, key=abs)
        # тикер применения обязан быть в ЗАГОЛОВКЕ, не в теле
        title_tickers = match_tickers(title)
        if not title_tickers:
            continue
        for tk in title_tickers:
            if abs(alpha) > abs(alphas.get(tk, 0.0)):
                alphas[tk] = alpha
    return alphas


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


def _vol_flag(vol_ratio: float | None) -> str:
    """Индикатор объёма утренней сессии относительно вчерашнего дневного.
    Утренняя сессия ~3ч из 9-часовой основной = ~33% времени.
    """
    if vol_ratio is None:
        return ""
    if vol_ratio >= 0.80:
        return "🔥"
    if vol_ratio >= 0.40:
        return "⬆️"
    if vol_ratio >= 0.20:
        return ""
    return "⬇️"


def _fmt_reinforced(ticker: str, overnight_pct: float, morning_pct: float | None,
                     status: str, conf: str, vol_flag: str = "",
                     status_emoji: str = "", news_alpha: float = 0.0) -> str:
    """TICKER  ±прог  ±утро  статус (уверенность) [эмодзи+📰 в конце]"""
    tk = f"{ticker:<6s}"
    if morning_pct is None:
        signals = f"{overnight_pct:+5.1f}      "
    else:
        signals = f"{overnight_pct:+5.1f} {morning_pct:+5.1f}"
    parts: list[str] = []
    if status_emoji:
        parts.append(status_emoji)
    if abs(news_alpha) >= 1.0:
        parts.append("📰")
    if vol_flag:
        parts.append(vol_flag)
    trailer = f"  {' '.join(parts)}" if parts else ""
    return f"{tk} {signals}  {status:<12s} ({conf}){trailer}"


async def build_briefing(day: date | None = None) -> str:
    day = day or datetime.now(MSK).date()

    fc, det = await factor_changes_overnight(day)
    overn = det["overnight_inputs_pct"]
    preds = det["predictions"]
    imoex_pct = preds.get("IMOEX", {}).get("expected_pct")
    overnight_bright = any(overn.get(k, 0) > 1.0 for k in ("SPX", "NIKKEI", "GOLD_LIVE", "BRENT_LIVE"))

    ranks = rank_expected_moves(fc)

    # === Морнинг-сессия для ВСЕХ тикеров ===
    all_ids = [r["secid"] for r in ranks]
    morn = {}
    morn_vol_ratio: dict[str, float | None] = {}
    try:
        morn_stats = await morning_for(all_ids, day, concurrency=6)
        for t, s in morn_stats.items():
            if s.morn_vs_prev_pct is not None:
                morn[t] = s.morn_vs_prev_pct
                morn_vol_ratio[t] = s.vol_ratio
        log.info("Утренняя сессия: %d бумаг из %d торговались", len(morn), len(all_ids))
    except Exception as e:
        log.warning("morning fetch failed: %s", e)

    # === News-alpha: применяем event-driven буст ДО блендинга с морнингом ===
    news_alphas = _news_alpha_map(day)
    for r in ranks:
        alpha = news_alphas.get(r["secid"], 0.0)
        r["news_alpha"] = alpha
        r["effective_pct"] = r["expected_open_pct"] + alpha

    # Блендинг с морнингом: final = 0.8*morn + 0.2*effective (когда есть морнинг)
    # Морнинг — уже случившийся факт, поэтому он должен доминировать.
    W_MORN = 0.8
    for r in ranks:
        m = morn.get(r["secid"])
        if m is not None:
            r["morning_pct"] = m
            r["final_pct"] = W_MORN * m + (1 - W_MORN) * r["effective_pct"]
        else:
            r["morning_pct"] = None
            r["final_pct"] = r["effective_pct"]

    ranks.sort(key=lambda x: x["final_pct"], reverse=True)

    with connect() as conn:
        def sector_of(t):
            r = conn.execute("SELECT sector_index, vol_daily FROM ticker_vol WHERE secid=?", (t,)).fetchone()
            return r["sector_index"] if r else None
        def vol_of(t):
            r = conn.execute("SELECT vol_daily FROM ticker_vol WHERE secid=?", (t,)).fetchone()
            return (r["vol_daily"] or 0) * 100 if r else 0

        ups = _diversified_pick(ranks, sector_of, max_per_sector=4, limit=15, positive_only=True, key="final_pct")
        downs_all = sorted(ranks, key=lambda x: x["final_pct"])
        downs = _diversified_pick(downs_all, sector_of, max_per_sector=3, limit=6, positive_only=False, key="final_pct")

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

    if ups:
        lines.append(f"<b>Топ-{len(ups)} вверх (≤4 на сектор)</b>")
        rows = []
        for r in ups:
            t = r["secid"]
            effective = r["effective_pct"]  # прог модели + news_alpha
            morn_ = r.get("morning_pct")
            alpha = r.get("news_alpha", 0.0)
            v = vol_of(t)
            status, conf = _reinforce_status(effective, morn_, v)
            vf = _vol_flag(morn_vol_ratio.get(t))
            emoji = "🟢" if status == "усилено" else ""
            rows.append(_fmt_reinforced(t, effective, morn_, status, conf, vf, emoji, alpha))
        lines.append("<pre>" + "\n".join(rows) + "</pre>")
    else:
        lines.append("<b>Топ вверх</b>")
        lines.append(f"Ни модели, ни утренняя сессия не дают бычьих сигналов. Overnight: Nikkei {ni:+.1f}%, золото {gd:+.1f}%.")
    lines.append("")

    strong_downs = [d for d in downs if d["final_pct"] <= -0.3]
    if len(strong_downs) >= 3:
        lines.append("<b>Топ вниз</b>")
        rows = []
        for r in downs:
            t = r["secid"]
            effective = r["effective_pct"]
            morn_ = r.get("morning_pct")
            alpha = r.get("news_alpha", 0.0)
            v = vol_of(t)
            status, conf = _reinforce_status(effective, morn_, v)
            vf = _vol_flag(morn_vol_ratio.get(t))
            emoji = "🟢" if status == "усилено" else ""
            rows.append(_fmt_reinforced(t, effective, morn_, status, conf, vf, emoji, alpha))
        lines.append("<pre>" + "\n".join(rows) + "</pre>")
        lines.append("")

    # Разбор новостей: только для реально больших ходов (>2%)
    big_movers = set()
    for r in ups[:6]:
        m = r.get("morning_pct")
        if m is not None and m >= 2.0:
            big_movers.add(r["secid"])
    for r in downs[:6]:
        m = r.get("morning_pct")
        if m is not None and m <= -2.0:
            big_movers.add(r["secid"])
    if big_movers:
        move_news = _find_news_for_tickers(big_movers, day)
        lines.append("<b>Что стоит за движением</b>")
        src_map = {
            "www.kommersant.ru": "Ъ", "www.rbc.ru": "РБК", "www.vedomosti.ru": "Вед",
            "www.interfax.ru": "И-факс", "1prime.ru": "Прайм", "www.moex.com": "MOEX",
        }
        for tk in sorted(big_movers, key=lambda t: abs(morn.get(t, 0) or 0), reverse=True):
            morn_val = morn.get(tk)
            move_hint = f"{morn_val:+.1f}%" if morn_val is not None else "?"
            short_name = _short(tk)
            item = move_news.get(tk)
            if item:
                src = item["url"].split("/")[2] if item["url"].startswith("http") else "src"
                src_name = src_map.get(src, src)
                lines.append(f"• <b>{short_name} ({tk})</b> {move_hint} — {item['title'][:130]} <a href=\"{item['url']}\">{src_name}</a>")
            else:
                lines.append(f"• <b>{short_name} ({tk})</b> {move_hint} — <i>новостей в базе нет, стоит проверить руками</i>")
        lines.append("")

    # Отдельно — pre-open новости с сильными тегами (уже верифицированы заголовком)
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

    lines.append("<i>Числа: 1-е — прогноз (модель + новостной эффект), 2-е — утренняя сессия (факт).</i>")
    lines.append("<i>Эмодзи: 🟢 утро усилило прогноз, 📰 в прогнозе учтён новостной эффект, 🔥/⬆️/⬇️ объём утра.</i>")
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
