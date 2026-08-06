"""Маппинг тикер → сектор-индекс MOEX через ISS analytics."""
from __future__ import annotations

import asyncio
import logging

from .http import client

log = logging.getLogger(__name__)

SECTOR_INDICES = [
    "MOEXOG",   # нефть/газ
    "MOEXMM",   # металлы/добыча
    "MOEXFN",   # финансы
    "MOEXCN",   # потребительский
    "MOEXIT",   # IT
    "MOEXTLC",  # телеком (актуальное название)
    "MOEXTN",   # транспорт
    "MOEXEU",   # электроэнергетика
    "MOEXRE",   # недвижимость
    "MOEXCH",   # химия/удобрения
]

ISS_ANALYTICS = "https://iss.moex.com/iss/statistics/engines/stock/markets/index/analytics/{idx}/securities.json"


async def _fetch_index_members(idx: str) -> list[tuple[str, float]]:
    async with client() as c:
        r = await c.get(ISS_ANALYTICS.format(idx=idx), params={"iss.meta": "off"})
        if r.status_code != 200:
            return []
        data = r.json().get("analytics", {})
    cols = data.get("columns", [])
    if not cols:
        return []
    ci = {n: i for i, n in enumerate(cols)}
    out: list[tuple[str, float]] = []
    seen = set()
    for row in data.get("data", []):
        secid = row[ci["ticker"]]
        if secid in seen:
            continue
        seen.add(secid)
        w = row[ci["weight"]] if "weight" in ci else 0.0
        out.append((secid, float(w or 0.0)))
    return out


async def sector_map() -> dict[str, str]:
    """Вернуть {SECID: sector_index}. Если тикер в нескольких индексах — берём с большим весом."""
    result: dict[str, tuple[str, float]] = {}
    members = await asyncio.gather(
        *(_fetch_index_members(idx) for idx in SECTOR_INDICES),
        return_exceptions=True,
    )
    for idx, memb in zip(SECTOR_INDICES, members):
        if isinstance(memb, Exception):
            log.warning("Sector %s: %s", idx, memb)
            continue
        for secid, weight in memb:
            prev = result.get(secid)
            if prev is None or weight > prev[1]:
                result[secid] = (idx, weight)
    return {sec: idx for sec, (idx, _) in result.items()}


def main() -> None:
    import json
    logging.basicConfig(level=logging.INFO)
    m = asyncio.run(sector_map())
    for sec, idx in sorted(m.items()):
        print(f"  {sec:8s} → {idx}")
    print(f"\nВсего тикеров с сектором: {len(m)}")


if __name__ == "__main__":
    main()
