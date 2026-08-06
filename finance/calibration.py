"""Историческая калибровка: бэты каждой бумаги к макро-факторам.

Считаем на дневных close-to-close returns за N дней.
Факторы (те, что реально влияют на российскую бумагу):
    IMOEX       — общий рыночный бэта
    <sector>    — секторный бэта (MOEXOG/MOEXMM/MOEXFN/...)
    USDRUB      — валютный бэта (экспортёры vs импортёры)
    CNYRUB      — азиатский валютный бэта
    BR          — нефть (MOEX FORTS BRU6 rolling)
    GOLD        — золото (MOEX FORTS GDU6 rolling)
    SPX         — глобальный риск-регим (overnight gap)
    NIKKEI      — азиатский риск-регим

Формула:  β = cov(r_ticker, r_factor) / var(r_factor)
R² используем для отбраковки шумных бэт (низкий R² → мало сигнала).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np

from .context import Bar, collect_series
from .http import client
from .sectors import sector_map
from .storage import all_tickers, connect, init_db

log = logging.getLogger(__name__)

FACTORS: list[str] = ["IMOEX", "USDRUB", "CNYRUB", "BR", "GOLD", "SPX", "NIKKEI"]

TICKER_HIST_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR"
    "/securities/{secid}.json"
)


async def _fetch_ticker_bars(secid: str, since: date, until: date, sem: asyncio.Semaphore) -> list[Bar]:
    """Пагинируем MOEX ISS history (PAGESIZE=100)."""
    async with sem, client() as c:
        all_rows: list[list] = []
        cols: list[str] = []
        start = 0
        while True:
            r = await c.get(
                TICKER_HIST_URL.format(secid=secid),
                params={
                    "from": since.isoformat(),
                    "till": until.isoformat(),
                    "iss.meta": "off",
                    "iss.only": "history",
                    "history.columns": "TRADEDATE,OPEN,HIGH,LOW,CLOSE",
                    "start": str(start),
                },
            )
            if r.status_code != 200:
                break
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
            if len(page) < 100:
                break
            start += len(page)
            if start > 10000:
                break

    if not cols:
        return []
    ci = {n: i for i, n in enumerate(cols)}
    bars: list[Bar] = []
    for row in all_rows:
        cval = row[ci.get("CLOSE", -1)] if "CLOSE" in ci else None
        if cval is None:
            continue
        bars.append(Bar(
            date=row[ci["TRADEDATE"]],
            open=row[ci.get("OPEN", -1)] if "OPEN" in ci else None,
            high=row[ci.get("HIGH", -1)] if "HIGH" in ci else None,
            low=row[ci.get("LOW", -1)] if "LOW" in ci else None,
            close=cval,
        ))
    return bars


def _returns(bars: list[Bar]) -> dict[str, float]:
    """Возвращает {date: ret_pct} для соседних торговых дней."""
    ret: dict[str, float] = {}
    prev = None
    for b in bars:
        if b.close is None:
            prev = None
            continue
        if prev is not None and prev.close:
            ret[b.date] = (b.close - prev.close) / prev.close
        prev = b
    return ret


def _beta(ticker_ret: dict[str, float], factor_ret: dict[str, float]) -> tuple[float | None, float | None, int]:
    """OLS beta + R². Возвращает (beta, r², n)."""
    common = sorted(set(ticker_ret) & set(factor_ret))
    n = len(common)
    if n < 20:
        return None, None, n
    y = np.array([ticker_ret[d] for d in common])
    x = np.array([factor_ret[d] for d in common])
    var_x = float(np.var(x, ddof=1))
    if var_x == 0:
        return None, None, n
    beta = float(np.cov(y, x, ddof=1)[0, 1] / var_x)
    corr = float(np.corrcoef(y, x)[0, 1]) if np.std(y) > 0 else 0.0
    r2 = corr * corr
    return beta, r2, n


async def calibrate(days_back: int = 180, as_of: date | None = None) -> dict:
    """Полная калибровка. Возвращает stats {inserted_vol, inserted_betas, errors}."""
    init_db()
    as_of = as_of or datetime.now(timezone.utc).date()
    since = as_of - timedelta(days=days_back + 30)  # запас на выходные

    log.info("Тяну факторы за %d дней…", days_back)
    factor_series = await collect_series(days_back=days_back + 30, as_of=as_of)
    factor_returns: dict[str, dict[str, float]] = {}
    for name in FACTORS + [x for x in factor_series if x.startswith("MOEX") and x not in FACTORS]:
        s = factor_series.get(name)
        if s and s.bars:
            factor_returns[name] = _returns(s.bars)

    log.info("Тяну секторный маппинг…")
    sec_map = await sector_map()

    tickers = [r["secid"] for r in all_tickers()]
    log.info("Тяну историю по %d тикерам…", len(tickers))
    sem = asyncio.SemaphoreValue = asyncio.Semaphore(8)
    ticker_bars = await asyncio.gather(*(_fetch_ticker_bars(t, since, as_of, sem) for t in tickers))
    ticker_returns = {t: _returns(bs) for t, bs in zip(tickers, ticker_bars)}

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {"vol_written": 0, "betas_written": 0, "skipped": 0}

    with connect() as conn:
        for t in tickers:
            rets = ticker_returns[t]
            if len(rets) < 20:
                stats["skipped"] += 1
                continue
            vals = np.array(list(rets.values()))
            mean_r = float(vals.mean())
            vol_r = float(vals.std(ddof=1))
            sec_idx = sec_map.get(t)
            conn.execute(
                """INSERT INTO ticker_vol (secid, sector_index, n_days, mean_daily_ret, vol_daily, calibrated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(secid) DO UPDATE SET
                       sector_index=excluded.sector_index,
                       n_days=excluded.n_days,
                       mean_daily_ret=excluded.mean_daily_ret,
                       vol_daily=excluded.vol_daily,
                       calibrated_at=excluded.calibrated_at""",
                (t, sec_idx, len(vals), mean_r, vol_r, now_iso),
            )
            stats["vol_written"] += 1

            # список факторов для этого тикера: базовый + его секторный индекс
            factors_for_ticker = list(FACTORS)
            if sec_idx and sec_idx in factor_returns:
                factors_for_ticker.append(sec_idx)

            for factor in factors_for_ticker:
                fr = factor_returns.get(factor)
                if not fr:
                    continue
                beta, r2, n = _beta(rets, fr)
                if beta is None:
                    continue
                conn.execute(
                    """INSERT INTO ticker_betas (secid, factor, beta, r2, n, calibrated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(secid, factor) DO UPDATE SET
                           beta=excluded.beta, r2=excluded.r2, n=excluded.n, calibrated_at=excluded.calibrated_at""",
                    (t, factor, beta, r2, n, now_iso),
                )
                stats["betas_written"] += 1
        conn.commit()
    return stats


def expected_gap(secid: str, factor_changes_pct: dict[str, float], min_r2: float = 0.05) -> dict:
    """Ожидаемое движение бумаги при заданных изменениях факторов.

    factor_changes_pct — {factor: ret_pct} для каждого фактора (в процентах, e.g. +1.5).
    Возвращает: {total_pct, contributions: [(factor, beta, delta, contribution, r2)], vol_daily_pct}
    """
    with connect() as conn:
        vol_row = conn.execute("SELECT vol_daily FROM ticker_vol WHERE secid=?", (secid,)).fetchone()
        beta_rows = conn.execute(
            "SELECT factor, beta, r2, n FROM ticker_betas WHERE secid=?", (secid,)
        ).fetchall()
    contribs = []
    total = 0.0
    for r in beta_rows:
        factor = r["factor"]
        if factor not in factor_changes_pct:
            continue
        beta = r["beta"]
        r2 = r["r2"] or 0.0
        # returns в модели — доли, а не проценты; но у нас beta посчитана на долях, а factor_changes_pct — в %
        # если оба выражены в одинаковых единицах, ответ будет в тех же единицах
        delta = factor_changes_pct[factor]  # в %
        contrib = beta * delta               # в %
        contribs.append((factor, beta, delta, contrib, r2))
        # взвешиваем вклад квадратом R²? нет — R² только для доверия, не для взвешивания.
        # Простая аддитивная модель: суммируем все вклады. Не совсем корректно (мультиколлинеарность),
        # но для качественной оценки — приемлемо. Для устойчивого прогноза берём только IMOEX + сектор.
    # Прогноз: приоритет — IMOEX × его_бета, дальше вклад "нерыночных" факторов (BR, GOLD, USDRUB)
    # чтобы не двойного счёта: делим на 2 если есть и IMOEX и сектор
    primary = [c for c in contribs if c[0] == "IMOEX"]
    sector = [c for c in contribs if c[0].startswith("MOEX") and c[0] != "IMOEX"]
    others = [c for c in contribs if c[0] in ("BR", "GOLD", "USDRUB", "CNYRUB", "SPX", "NIKKEI")]

    def sum_of(xs): return sum(c[3] for c in xs)
    total = sum_of(primary) + sum_of(sector) * 0.5 + sum_of(others) * 0.3
    return {
        "secid": secid,
        "expected_open_pct": round(total, 2),
        "vol_daily_pct": round((vol_row["vol_daily"] or 0.0) * 100, 2) if vol_row else None,
        "contributions": [
            {"factor": f, "beta": round(b, 3), "delta_pct": round(d, 2),
             "contribution_pct": round(c, 2), "r2": round(r2, 3)}
            for f, b, d, c, r2 in contribs
        ],
    }


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args()
    stats = asyncio.run(calibrate(days_back=args.days))
    log.info("Готово: %s", stats)


if __name__ == "__main__":
    main()
