"""Overnight-регрессия: предсказываем сегодняшние MOEX-факторы из вчерашних глобальных.

Модель:
    r_TARGET(D) = c_0 + Σ c_i · r_PREDICTOR_i(D-1)

Target-факторы (то, что предсказываем на утро MOEX D):
    IMOEX, MOEXOG, MOEXMM, MOEXFN, MOEXCN, MOEXIT, MOEXTLC, MOEXTN, MOEXEU, USDRUB, CNYRUB

Overnight-предикторы (то, что известно к 10:00 МСК дня D):
    SPX_prev, NIKKEI_prev, HSI_prev, BRENT_LIVE_prev, GOLD_LIVE_prev, VIX_prev

Для честности берём _prev = «последняя доступная запись строго до даты D».
Nikkei технически закрывается 09:00 МСК того же D — можно было бы брать D-запись,
но чтобы не ловить edge-cases и обеспечить одинаковую обработку в тренировке и в бою,
универсально используем «last known before D».
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np

from .context import Bar, collect_series
from .storage import connect, init_db

log = logging.getLogger(__name__)

TARGETS = [
    "IMOEX", "MOEXOG", "MOEXMM", "MOEXFN", "MOEXCN", "MOEXIT",
    "MOEXTLC", "MOEXTN", "MOEXEU", "USDRUB", "CNYRUB",
]

# Осознанно не берём DJI: почти 100% коррелирует с SPX и создаёт мультиколлинеарность.
# Держим SPX как единый прокси на США.
PREDICTORS = [
    "SPX", "NIKKEI", "HSI", "BRENT_LIVE", "GOLD_LIVE", "VIX",
]


def _returns_map(bars: list[Bar]) -> dict[str, float]:
    r: dict[str, float] = {}
    prev = None
    for b in bars:
        if b.close is None:
            prev = None
            continue
        if prev and prev.close:
            r[b.date] = (b.close - prev.close) / prev.close
        prev = b
    return r


def _last_before(dates_sorted: list[str], target_date: str) -> str | None:
    """Двоичный поиск последней даты строго < target_date."""
    lo, hi = 0, len(dates_sorted)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates_sorted[mid] < target_date:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    return dates_sorted[lo - 1]


def _fit(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, float, int]:
    """OLS: y = a + X·b. Возвращает (coefs с intercept первым, R², n)."""
    n = len(y)
    if n < 30:
        return np.zeros(X.shape[1] + 1), 0.0, n
    Xb = np.column_stack([np.ones(n), X])  # добавляем intercept
    coef, *_ = np.linalg.lstsq(Xb, y, rcond=None)
    yhat = Xb @ coef
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return coef, r2, n


async def fit_overnight_predictors(days_back: int = 180, as_of: date | None = None) -> dict:
    """Обучить регрессию для каждого target и сохранить в таблицу."""
    init_db()
    as_of = as_of or datetime.now(timezone.utc).date()

    log.info("Тяну все серии…")
    ser = await collect_series(days_back=days_back + 60, as_of=as_of)

    # Returns per series
    all_returns: dict[str, dict[str, float]] = {}
    for name, s in ser.items():
        if s.bars:
            all_returns[name] = _returns_map(s.bars)

    # Предикторы + их отсортированные даты для _last_before
    pred_dates: dict[str, list[str]] = {}
    for p in PREDICTORS:
        if p in all_returns:
            pred_dates[p] = sorted(all_returns[p].keys())

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: dict[str, dict] = {}

    with connect() as conn:
        for tgt in TARGETS:
            tgt_ret = all_returns.get(tgt, {})
            if len(tgt_ret) < 30:
                log.warning("Target %s: недостаточно данных (%d)", tgt, len(tgt_ret))
                continue

            # Собираем матрицу X и вектор y
            rows_y: list[float] = []
            rows_X: list[list[float]] = []
            for d_str in sorted(tgt_ret.keys()):
                row: list[float | None] = []
                complete = True
                for p in PREDICTORS:
                    if p not in pred_dates:
                        row.append(0.0)
                        continue
                    prev_date = _last_before(pred_dates[p], d_str)
                    if prev_date is None:
                        complete = False
                        break
                    row.append(all_returns[p][prev_date])
                if not complete:
                    continue
                rows_y.append(tgt_ret[d_str])
                rows_X.append(row)

            if len(rows_y) < 30:
                log.warning("Target %s: после выравнивания только %d точек", tgt, len(rows_y))
                continue

            y = np.array(rows_y)
            X = np.array(rows_X)
            coef, r2, n = _fit(y, X)
            intercept = float(coef[0])
            coefs = {p: float(c) for p, c in zip(PREDICTORS, coef[1:])}

            conn.execute(
                """INSERT INTO index_predictors
                       (target, intercept, coef_json, r2, n_obs, predictors, calibrated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(target) DO UPDATE SET
                       intercept=excluded.intercept,
                       coef_json=excluded.coef_json,
                       r2=excluded.r2,
                       n_obs=excluded.n_obs,
                       predictors=excluded.predictors,
                       calibrated_at=excluded.calibrated_at""",
                (
                    tgt, intercept, json.dumps(coefs, ensure_ascii=False),
                    r2, n, json.dumps(PREDICTORS), now_iso,
                ),
            )
            results[tgt] = {"intercept_pct": round(intercept * 100, 3), "r2": round(r2, 3), "n": n, "coefs": coefs}
        conn.commit()
    return results


def predict_indices(predictor_changes_pct: dict[str, float]) -> dict[str, dict]:
    """Применить обученную регрессию: {predictor: Δ%} → {target: Δ%}.

    predictor_changes_pct — в процентах (например SPX = +0.5).
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT target, intercept, coef_json, r2, n_obs FROM index_predictors"
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        coefs = json.loads(r["coef_json"])
        # intercept + Σ coef_i · Δ_i  (в долях; на выходе — в %)
        # coef_i хранится для регрессии в долях, Δ_i приходит в %
        # ret_i = Δ_i / 100. yhat = a + Σ c_i · ret_i. Возвращаем yhat * 100.
        yhat = (r["intercept"] or 0.0) * 1.0
        contribs = {}
        for p, c in coefs.items():
            if p in predictor_changes_pct:
                d = predictor_changes_pct[p] / 100.0  # % → доля
                contribution = c * d
                yhat += contribution
                contribs[p] = {"coef": round(c, 4), "delta_pct": round(predictor_changes_pct[p], 3),
                                "contribution_pct": round(contribution * 100, 3)}
        out[r["target"]] = {
            "expected_pct": round(yhat * 100, 3),
            "r2": round(r["r2"] or 0.0, 3),
            "n": r["n_obs"],
            "contributions": contribs,
        }
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--show", action="store_true", help="Показать сохранённые коэффициенты")
    args = ap.parse_args()

    if args.show:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM index_predictors").fetchall()
        for r in rows:
            print(f"\n{r['target']}  R²={r['r2']:.3f}  n={r['n_obs']}  intercept={r['intercept']*100:+.3f}%/день")
            for p, c in json.loads(r["coef_json"]).items():
                print(f"   {p:11s} coef={c:+.3f}")
        return

    stats = asyncio.run(fit_overnight_predictors(days_back=args.days))
    for tgt, s in stats.items():
        print(f"{tgt:8s}  R²={s['r2']:.3f}  n={s['n']}  a={s['intercept_pct']:+.3f}%")
        for p, c in s["coefs"].items():
            if abs(c) > 0.01:
                print(f"   {p:11s} = {c:+.3f}")


if __name__ == "__main__":
    main()
