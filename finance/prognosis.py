"""Прогноз ожидаемого гэпа на основе исторических бет и текущих движений факторов.

Использование:
    py -m finance.prognosis                     # live-режим (pre-open)
    py -m finance.prognosis --backtest 2026-08-05  # ex-post оценка

В live-режиме факторы: разница между вчерашним закрытием MOEX и текущими live-ценами
(Yahoo Brent, Gold, Nikkei — торгуются, когда MOEX закрыт).
В backtest-режиме факторы: close-to-close изменение (день D vs D-1).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from .calibration import expected_gap
from .context import Bar, collect_series
from .regression import PREDICTORS as OVERNIGHT_PREDICTORS, predict_indices
from .storage import connect

log = logging.getLogger(__name__)


async def _get_bar(name: str, dt: date, span: int = 15):
    ser = await collect_series(days_back=span, as_of=dt)
    return ser.get(name)


async def factor_changes_backtest(day: date) -> dict[str, float]:
    """Δ % факторов за D vs D-1 (для ex-post оценки).

    Использует close-to-close. Это условно 'идеальные' overnight-данные —
    для реалистичного прогноза нужны live-версии факторов на pre-open.
    """
    ser = await collect_series(days_back=15, as_of=day)
    result: dict[str, float] = {}
    for name, s in ser.items():
        bars = [b for b in s.bars if b.date <= day.isoformat()]
        if len(bars) < 2:
            continue
        last = bars[-1]
        prev = bars[-2]
        if last.close and prev.close and prev.close > 0:
            result[name] = (last.close - prev.close) / prev.close * 100.0
    return result


async def overnight_predictor_changes(as_of: date) -> dict[str, float]:
    """Собрать Δ % overnight-предикторов (последняя запись строго ДО дня as_of).

    Используется как вход в regression.predict_indices → предсказание MOEX-факторов.
    """
    ser = await collect_series(days_back=15, as_of=as_of)
    out: dict[str, float] = {}
    as_of_str = as_of.isoformat()
    for p in OVERNIGHT_PREDICTORS:
        s = ser.get(p)
        if not s or len(s.bars) < 2:
            continue
        # берём последний бар с датой < as_of и предыдущий
        bars_before = [b for b in s.bars if b.date < as_of_str]
        if len(bars_before) < 2:
            continue
        last = bars_before[-1]; prev = bars_before[-2]
        if last.close and prev.close and prev.close > 0:
            out[p] = (last.close - prev.close) / prev.close * 100.0
    return out


async def factor_changes_overnight(as_of: date) -> tuple[dict[str, float], dict[str, dict]]:
    """Полный вход для честного pre-open прогноза.

    Возвращает (factor_changes_pct, prediction_details).
    factor_changes_pct содержит:
      1) Предсказанные MOEX-факторы (IMOEX, MOEXOG, MOEXMM, ..., USDRUB, CNYRUB) — из регрессии
      2) Overnight-известные сырьевые сигналы (BR, GOLD) — напрямую из ночных данных
         Без этого бэта тикера на BR/GOLD в модели умножается на 0 → сигнал теряется.
    """
    overnight = await overnight_predictor_changes(as_of)
    preds = predict_indices(overnight)
    factors_out: dict[str, float] = {}
    for target, info in preds.items():
        factors_out[target] = info["expected_pct"]

    # Пробрасываем сырьевые overnight-факторы напрямую
    # (calibration.py считает бэты BR, GOLD именно на MOEX-close-to-close, поэтому
    #  overnight-движение — прокси, но лучший из доступных до открытия MOEX)
    if "BRENT_LIVE" in overnight:
        factors_out["BR"] = overnight["BRENT_LIVE"]
    if "GOLD_LIVE" in overnight:
        factors_out["GOLD"] = overnight["GOLD_LIVE"]

    return factors_out, {"overnight_inputs_pct": overnight, "predictions": preds}


async def factor_changes_live(as_of: datetime | None = None) -> dict[str, float]:
    """Δ % факторов между вчерашним закрытием MOEX и текущими live-значениями.

    Для факторов, торгующихся ночью (Yahoo BRENT_LIVE, GOLD_LIVE, NIKKEI),
    берём (yahoo_last - moex_prev_close) / moex_prev_close.
    Для «спящих» MOEX-факторов берём предыдущий Δ (D-1 vs D-2) как best guess.
    """
    as_of = as_of or datetime.now(timezone.utc)
    ser = await collect_series(days_back=15, as_of=as_of.date())
    out: dict[str, float] = {}
    # для MOEX-факторов, у которых нет ночного трейдинга — берём последний close-to-close
    for name in ("IMOEX", "RTSI", "MOEXOG", "MOEXMM", "MOEXFN", "MOEXCN",
                 "MOEXIT", "MOEXTL", "MOEXTN", "MOEXEU", "USDRUB", "CNYRUB"):
        s = ser.get(name)
        if s and len(s.bars) >= 2 and s.bars[-1].close and s.bars[-2].close:
            out[name] = (s.bars[-1].close - s.bars[-2].close) / s.bars[-2].close * 100.0

    # Brent: live Yahoo BZ=F vs MOEX BR close вчера
    br = ser.get("BR"); brlive = ser.get("BRENT_LIVE")
    if br and brlive and br.bars and brlive.bars:
        prev = br.bars[-1].close
        live = brlive.bars[-1].close
        if prev and live and prev > 0:
            out["BR"] = (live - prev) / prev * 100.0

    # Gold: live GC=F vs MOEX GOLD
    gd = ser.get("GOLD"); gdlive = ser.get("GOLD_LIVE")
    if gd and gdlive and gd.bars and gdlive.bars:
        prev = gd.bars[-1].close
        live = gdlive.bars[-1].close
        if prev and live and prev > 0:
            out["GOLD"] = (live - prev) / prev * 100.0

    # SPX, NIKKEI, HSI — просто последний D-vs-D-1
    for name in ("SPX", "NIKKEI", "HSI", "DJI"):
        s = ser.get(name)
        if s and len(s.bars) >= 2 and s.bars[-1].close and s.bars[-2].close:
            out[name] = (s.bars[-1].close - s.bars[-2].close) / s.bars[-2].close * 100.0
    return out


def rank_expected_moves(factor_changes: dict[str, float], min_vol_pct: float = 0.5) -> list[dict]:
    """Для всех тикеров с калибровкой посчитать ожидаемый гэп; отсортировать по |Δ|."""
    with connect() as conn:
        rows = conn.execute("SELECT secid FROM ticker_vol WHERE vol_daily >= ?", (min_vol_pct / 100.0,)).fetchall()
    tickers = [r["secid"] for r in rows]
    out = []
    for t in tickers:
        eg = expected_gap(t, factor_changes)
        if eg.get("expected_open_pct") is None:
            continue
        out.append(eg)
    out.sort(key=lambda x: abs(x["expected_open_pct"]), reverse=True)
    return out


def format_ranked(ranks: list[dict], factor_changes: dict[str, float], top: int = 25) -> str:
    lines = []
    lines.append("## Ожидаемые гэпы (top-{} по |Δ|)".format(top))
    lines.append("")
    lines.append("Модель:  Δ_ожид = β_IMOEX·Δ_IMOEX + 0.5·β_сектор·Δ_сектор + 0.3·Σ β_i·Δ_i (макро)")
    lines.append("")
    lines.append("**Использованные Δ факторов:**")
    lines.append("| Фактор | Δ % |")
    lines.append("|---|---:|")
    for f, v in sorted(factor_changes.items()):
        lines.append(f"| {f} | {v:+.2f}% |")
    lines.append("")
    lines.append("**Топ ожидаемых движений:**")
    lines.append("| Тикер | Ожид. Δ | Vol дн. | Основные вклады |")
    lines.append("|---|---:|---:|---|")
    for r in ranks[:top]:
        contribs = [
            f"{c['factor']}({c['contribution_pct']:+.2f})"
            for c in r["contributions"]
            if abs(c["contribution_pct"]) >= 0.1 and c["r2"] >= 0.05
        ]
        vol = r.get("vol_daily_pct")
        vol_s = f"{vol:.2f}%" if vol is not None else "—"
        lines.append(f"| {r['secid']} | {r['expected_open_pct']:+.2f}% | {vol_s} | {', '.join(contribs) or '—'} |")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", type=str, help="YYYY-MM-DD: считать факторы close-to-close (v2 attribution)")
    ap.add_argument("--overnight", type=str, help="YYYY-MM-DD: честный pre-open прогноз через overnight-регрессию")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    details = None
    if args.overnight:
        day = datetime.strptime(args.overnight, "%Y-%m-%d").date()
        fc, details = asyncio.run(factor_changes_overnight(day))
    elif args.backtest:
        day = datetime.strptime(args.backtest, "%Y-%m-%d").date()
        fc = asyncio.run(factor_changes_backtest(day))
    else:
        fc = asyncio.run(factor_changes_live())

    ranks = rank_expected_moves(fc)
    print(format_ranked(ranks, fc, top=args.top))

    if details:
        print("\n\n## Overnight-вход и предсказания индексов\n")
        print("**Overnight predictor changes:**")
        for p, v in sorted(details["overnight_inputs_pct"].items()):
            print(f"- {p}: {v:+.3f}%")
        print("\n**Predicted MOEX factors (регрессия):**")
        for tgt, info in details["predictions"].items():
            print(f"- {tgt}: {info['expected_pct']:+.3f}%  (R²={info['r2']}, n={info['n']})")


if __name__ == "__main__":
    main()
