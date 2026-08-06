"""Утренняя сессия MOEX 06:50–09:50 МСК — главный pre-open сигнал.

Один HTTP-запрос на тикер, тянем candles за 2 дня (вчерашняя основная + утро сегодня).
Вычисляем vs_prev_close = (last_morning_close − prev_day_regular_close) / prev.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .http import client

log = logging.getLogger(__name__)

CANDLES_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
    "/securities/{secid}/candles.json"
)


@dataclass
class MorningStats:
    secid: str
    morn_return_pct: float | None    # (last_morn_close - first_morn_open) / first_morn_open
    morn_vs_prev_pct: float | None   # (last_morn_close - prev_day_close) / prev_day_close
    morn_volume: int | None
    morn_vwap: float | None
    n_bars: int
    prev_day_volume: int | None = None     # объём вчерашнего полного торгового дня
    vol_ratio: float | None = None         # утренний объём / вчерашний дневной


async def _fetch_ticker_morning(secid: str, day: date, sem: asyncio.Semaphore,
                                 shared_client) -> MorningStats:
    empty = MorningStats(secid=secid, morn_return_pct=None, morn_vs_prev_pct=None,
                         morn_volume=None, morn_vwap=None, n_bars=0)
    # Берём 3 календарных дня назад: пятница/пн для покрытия выходных
    since = day - timedelta(days=4)
    async with sem:
        try:
            r = await shared_client.get(
                CANDLES_URL.format(secid=secid),
                params={
                    "from": since.isoformat(),
                    "till": day.isoformat(),
                    "interval": "10",
                    "iss.meta": "off",
                    "iss.only": "candles",
                },
            )
        except Exception as e:
            log.debug("morning %s: %s", secid, e)
            return empty
    if r.status_code != 200:
        return empty
    try:
        data = r.json().get("candles", {}).get("data", []) or []
    except Exception:
        return empty
    # cols: [open, close, high, low, value, volume, begin, end]
    day_s = day.isoformat()
    morning_boundary = f"{day_s} 10:00:00"
    morn = [row for row in data if row[6].startswith(day_s) and row[6] < morning_boundary]
    prior = [row for row in data if row[6] < day_s]
    if not morn:
        return empty
    first_open = morn[0][0]
    last_close = morn[-1][1]
    total_value = sum((x[4] or 0) for x in morn)
    total_vol = sum((x[5] or 0) for x in morn)
    vwap = (total_value / total_vol) if total_vol else None

    morn_return = None
    if first_open and last_close and first_open > 0:
        morn_return = (last_close - first_open) / first_open * 100.0

    morn_vs_prev = None
    prev_day_vol = None
    vol_ratio = None
    if prior:
        prev_close = prior[-1][1]
        if prev_close and last_close and prev_close > 0:
            morn_vs_prev = (last_close - prev_close) / prev_close * 100.0
        # Объём последнего торгового дня в prior
        last_prior_day = prior[-1][6][:10]
        prev_day_bars = [b for b in prior if b[6].startswith(last_prior_day)]
        prev_day_vol = int(sum((b[5] or 0) for b in prev_day_bars)) if prev_day_bars else None
        if prev_day_vol and total_vol and prev_day_vol > 0:
            vol_ratio = total_vol / prev_day_vol

    return MorningStats(
        secid=secid, morn_return_pct=morn_return, morn_vs_prev_pct=morn_vs_prev,
        morn_volume=int(total_vol) if total_vol else None, morn_vwap=vwap, n_bars=len(morn),
        prev_day_volume=prev_day_vol, vol_ratio=vol_ratio,
    )


async def morning_for(tickers: list[str], day: date | None = None,
                       concurrency: int = 4) -> dict[str, MorningStats]:
    day = day or datetime.now().date()
    sem = asyncio.Semaphore(concurrency)
    async with client() as c:
        stats = await asyncio.gather(*(_fetch_ticker_morning(t, day, sem, c) for t in tickers),
                                     return_exceptions=False)
    return {s.secid: s for s in stats}


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None)
    ap.add_argument("tickers", nargs="+")
    args = ap.parse_args()
    d = date.fromisoformat(args.date) if args.date else date.today()
    stats = asyncio.run(morning_for(args.tickers, d))
    for t, s in stats.items():
        r = f"{s.morn_return_pct:+.2f}%" if s.morn_return_pct is not None else "—"
        vp = f"{s.morn_vs_prev_pct:+.2f}%" if s.morn_vs_prev_pct is not None else "—"
        print(f"  {t:6s}  bars={s.n_bars:2d}  ret={r}  vs_prev={vp}  vol={s.morn_volume}")


if __name__ == "__main__":
    main()
