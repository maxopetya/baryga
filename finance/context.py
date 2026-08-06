"""Внешний контекст: FX, товарные фьючерсы, мировые индексы.

Даёт снимок макро-факторов на дату/время и historical time-series по каждому.

Источники:
- MOEX ISS: FX (USD/RUB, CNY/RUB), индексы (IMOEX, RTSI, сектор-индексы), FORTS (Brent, Gold)
- Yahoo Finance: мировые индексы (S&P, Nikkei, Hang Seng), live-котировки Brent/Gold
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from .http import client

log = logging.getLogger(__name__)

# ── MOEX-факторы: (engine, market, board, secid или asset для FORTS) ──
MOEX_SIMPLE: dict[str, dict] = {
    # Индексы Мосбиржи (index engine)
    "IMOEX":  {"engine": "stock",    "market": "index", "board": None, "secid": "IMOEX"},
    "RTSI":   {"engine": "stock",    "market": "index", "board": None, "secid": "RTSI"},
    "MOEXOG": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXOG"},  # нефть-газ
    "MOEXMM": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXMM"},  # металлы/добыча
    "MOEXFN": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXFN"},  # финансы
    "MOEXCN": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXCN"},  # потреб
    "MOEXIT": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXIT"},  # IT
    "MOEXTL": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXTL"},  # телеком
    "MOEXTN": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXTN"},  # транспорт
    "MOEXEU": {"engine": "stock",    "market": "index", "board": None, "secid": "MOEXEU"},  # электроэнергетика
    # Валютный рынок MOEX (SELT), board CETS
    "USDRUB": {"engine": "currency", "market": "selt",  "board": "CETS", "secid": "USD000UTSTOM"},
    "CNYRUB": {"engine": "currency", "market": "selt",  "board": "CETS", "secid": "CNYRUB_TOM"},
}

# FORTS-факторы (нужен подбор активного контракта по assetcode)
MOEX_FORTS_ASSETS: dict[str, str] = {
    "BR":   "BR",     # Brent
    "GOLD": "GOLD",   # Gold
}

# Yahoo Finance ↔ label
YAHOO_SYMBOLS: dict[str, str] = {
    "SPX":         "^GSPC",     # S&P 500
    "DJI":         "^DJI",      # Dow Jones
    "NIKKEI":      "^N225",     # Nikkei 225
    "HSI":         "^HSI",      # Hang Seng
    "SHANGHAI":    "000001.SS", # Shanghai Composite
    "BRENT_LIVE":  "BZ=F",      # Brent Sep-контракт ICE (24-часовой)
    "GOLD_LIVE":   "GC=F",      # Gold спот-фьючерс COMEX
    "WTI_LIVE":    "CL=F",      # WTI
    "VIX":         "^VIX",      # Волатильность S&P
}

ISS_HISTORY = "https://iss.moex.com/iss/history/engines/{engine}/markets/{market}"
ISS_HISTORY_BOARD = "/boards/{board}"
ISS_HISTORY_SEC = "/securities/{secid}.json"

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={range}"


@dataclass
class Bar:
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None

    def as_dict(self) -> dict:
        return {"date": self.date, "open": self.open, "high": self.high, "low": self.low, "close": self.close}


@dataclass
class Series:
    name: str
    source: str          # "moex" | "yahoo"
    bars: list[Bar] = field(default_factory=list)

    def last(self, n: int = 1) -> list[Bar]:
        return self.bars[-n:] if self.bars else []

    def chg_pct(self, at: int = -1, prev: int = -2) -> float | None:
        try:
            a = self.bars[at].close
            b = self.bars[prev].close
            if a is None or b is None or b == 0:
                return None
            return (a - b) / b * 100.0
        except IndexError:
            return None


async def _fetch_moex_history(cfg: dict, since: date, until: date) -> list[Bar]:
    """MOEX ISS history с пагинацией. PAGESIZE=100, идём по start= пока страница полная."""
    engine, market, board, secid = cfg["engine"], cfg["market"], cfg.get("board"), cfg["secid"]
    url = ISS_HISTORY.format(engine=engine, market=market)
    if board:
        url += ISS_HISTORY_BOARD.format(board=board)
    url += ISS_HISTORY_SEC.format(secid=secid)

    all_rows: list[list] = []
    cols: list[str] = []
    start = 0
    async with client() as c:
        while True:
            params = {
                "from": since.isoformat(),
                "till": until.isoformat(),
                "iss.meta": "off",
                "iss.only": "history",
                "start": str(start),
            }
            r = await c.get(url, params=params)
            r.raise_for_status()
            d = r.json().get("history", {})
            page_cols = d.get("columns", [])
            if not page_cols:
                break
            if not cols:
                cols = page_cols
            page = d.get("data", [])
            if not page:
                break
            all_rows.extend(page)
            if len(page) < 100:  # PAGESIZE MOEX ISS = 100
                break
            start += len(page)
            if start > 10000:  # safety
                break

    if not cols:
        return []
    idx = {n: i for i, n in enumerate(cols)}
    bars: list[Bar] = []
    for row in all_rows:
        if board and "BOARDID" in idx and row[idx["BOARDID"]] != board:
            continue
        date_s = row[idx.get("TRADEDATE", 1)]
        get = lambda k: row[idx[k]] if k in idx else None
        bars.append(Bar(date=date_s, open=get("OPEN"), high=get("HIGH"), low=get("LOW"), close=get("CLOSE")))
    dedup: dict[str, Bar] = {}
    for b in bars:
        dedup[b.date] = b
    return sorted(dedup.values(), key=lambda b: b.date)


async def _resolve_forts_active(asset: str, as_of: date) -> str | None:
    """Найти ближайший активный фьючерс с указанным assetcode."""
    url = "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
    params = {
        "iss.meta": "off",
        "securities.columns": "SECID,ASSETCODE,LASTTRADEDATE",
    }
    async with client() as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        d = r.json()["securities"]
    cols = d["columns"]; idx = {n: i for i, n in enumerate(cols)}
    candidates = []
    for row in d["data"]:
        if row[idx["ASSETCODE"]] != asset:
            continue
        ltd = row[idx["LASTTRADEDATE"]]
        try:
            ltd_d = date.fromisoformat(ltd)
        except (TypeError, ValueError):
            continue
        if ltd_d >= as_of:
            candidates.append((ltd_d, row[idx["SECID"]]))
    if not candidates:
        return None
    candidates.sort()  # ближайший по экспирации
    return candidates[0][1]


async def _fetch_forts_history(asset: str, since: date, until: date) -> list[Bar]:
    secid = await _resolve_forts_active(asset, until)
    if not secid:
        log.warning("FORTS asset %s: активный контракт не найден", asset)
        return []
    cfg = {"engine": "futures", "market": "forts", "secid": secid}
    return await _fetch_moex_history(cfg, since, until)


async def _fetch_yahoo(sym: str, range_: str = "1mo") -> list[Bar]:
    url = YAHOO_CHART.format(sym=sym, range=range_)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as c:
        r = await c.get(url)
        if r.status_code != 200:
            return []
        try:
            data = r.json()
        except Exception:
            return []
    try:
        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        q = result["indicators"]["quote"][0]
        bars: list[Bar] = []
        for i, t in enumerate(ts):
            d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
            bars.append(Bar(
                date=d,
                open=q["open"][i] if q.get("open") else None,
                high=q["high"][i] if q.get("high") else None,
                low=q["low"][i] if q.get("low") else None,
                close=q["close"][i] if q.get("close") else None,
            ))
        return [b for b in bars if b.close is not None]
    except (KeyError, IndexError, TypeError):
        return []


async def collect_series(days_back: int = 30, as_of: date | None = None) -> dict[str, Series]:
    """Собрать все факторы за окно last N дней (для контекста и калибровки)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    since = as_of - timedelta(days=days_back)
    result: dict[str, Series] = {}

    # MOEX simple (индексы + FX)
    async def one_moex(name: str, cfg: dict) -> tuple[str, Series]:
        bars = await _fetch_moex_history(cfg, since, as_of)
        return name, Series(name=name, source="moex", bars=bars)

    # FORTS (нужен resolve контракта)
    async def one_forts(name: str, asset: str) -> tuple[str, Series]:
        bars = await _fetch_forts_history(asset, since, as_of)
        return name, Series(name=name, source="moex", bars=bars)

    # Yahoo
    async def one_yahoo(name: str, sym: str) -> tuple[str, Series]:
        # выбираем ближайший диапазон Yahoo к нашему days_back
        rng = ("1mo" if days_back <= 30 else "3mo" if days_back <= 90 else
               "6mo" if days_back <= 180 else "1y" if days_back <= 365 else
               "2y" if days_back <= 730 else "5y")
        bars = await _fetch_yahoo(sym, rng)
        # обрезаем по дате запроса
        bars = [b for b in bars if b.date <= as_of.isoformat()]
        return name, Series(name=name, source="yahoo", bars=bars)

    tasks = (
        [one_moex(n, cfg) for n, cfg in MOEX_SIMPLE.items()] +
        [one_forts(n, a)  for n, a in MOEX_FORTS_ASSETS.items()] +
        [one_yahoo(n, s)  for n, s in YAHOO_SYMBOLS.items()]
    )
    for coro in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(coro, Exception):
            log.warning("factor fetch failed: %s", coro)
            continue
        name, series = coro
        result[name] = series
    return result


async def snapshot(as_of: date | None = None) -> dict:
    """Пре-опен снимок: last close + change vs prev close по каждому фактору."""
    ser = await collect_series(days_back=10, as_of=as_of)
    out: dict[str, dict] = {}
    for name, s in ser.items():
        if not s.bars:
            out[name] = {"error": "no data"}
            continue
        last = s.bars[-1]
        chg = s.chg_pct()
        out[name] = {
            "date": last.date,
            "close": last.close,
            "open": last.open,
            "high": last.high,
            "low":  last.low,
            "chg_pct_1d": round(chg, 3) if chg is not None else None,
            "source": s.source,
        }
    return out


def format_snapshot(snap: dict) -> str:
    """Человекочитаемая табличка для markdown-брифинга."""
    lines = ["| Фактор | Дата | Close | Δ % (1d) |", "|---|---|---:|---:|"]
    order = [
        "IMOEX", "RTSI", "MOEXOG", "MOEXMM", "MOEXFN", "MOEXCN",
        "MOEXIT", "MOEXTL", "MOEXTN", "MOEXEU",
        "USDRUB", "CNYRUB",
        "BR", "GOLD",
        "SPX", "DJI", "NIKKEI", "HSI", "SHANGHAI",
        "BRENT_LIVE", "GOLD_LIVE", "WTI_LIVE", "VIX",
    ]
    for name in order:
        if name not in snap:
            continue
        s = snap[name]
        if "error" in s:
            lines.append(f"| {name} | — | — | _{s['error']}_ |")
            continue
        chg = s.get("chg_pct_1d")
        chg_s = f"{chg:+.2f}%" if chg is not None else "—"
        close = s.get("close")
        close_s = f"{close:.4g}" if close is not None else "—"
        lines.append(f"| {name} | {s['date']} | {close_s} | {chg_s} |")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    snap = asyncio.run(snapshot())
    print(format_snapshot(snap))


if __name__ == "__main__":
    main()
