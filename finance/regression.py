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
EXTERNAL_PREDICTORS = [
    "SPX", "NIKKEI", "HSI", "BRENT_LIVE", "GOLD_LIVE", "VIX",
]

# Momentum-фичи (внутренние, из IMOEX истории) — добавляют «где находится рынок».
# Известны к 09:30, потому что вычисляются из вчерашнего закрытия и глубже.
MOMENTUM_FEATURES = [
    "IMOEX_MOM_5D",     # доходность IMOEX за 5 последних торговых дней
    "IMOEX_MOM_20D",    # доходность IMOEX за 20 торговых дней (средний тренд)
    "IMOEX_VOL_20D",    # ст.откл. дневных ret'ов IMOEX за 20 дней (volatility regime)
]

PREDICTORS = EXTERNAL_PREDICTORS + MOMENTUM_FEATURES


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


def _momentum_features(imoex_returns: dict[str, float], as_of_date: str) -> dict[str, float | None]:
    """Momentum по IMOEX, ВСЁ строго до as_of_date (без look-ahead).

    IMOEX_MOM_5D  = cumulative log-ret за последние 5 торговых дней
    IMOEX_MOM_20D = cumulative log-ret за последние 20 торговых дней
    IMOEX_VOL_20D = ст.откл. дневных ret'ов за 20 дней
    """
    prior = sorted(d for d in imoex_returns if d < as_of_date)
    out: dict[str, float | None] = {"IMOEX_MOM_5D": None, "IMOEX_MOM_20D": None, "IMOEX_VOL_20D": None}
    if len(prior) < 5:
        return out
    last5 = prior[-5:]
    out["IMOEX_MOM_5D"] = sum(imoex_returns[d] for d in last5)
    if len(prior) < 20:
        return out
    last20 = prior[-20:]
    import statistics
    out["IMOEX_MOM_20D"] = sum(imoex_returns[d] for d in last20)
    out["IMOEX_VOL_20D"] = statistics.pstdev([imoex_returns[d] for d in last20])
    return out


def _fit(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, float, int]:
    """OLS: y = a + X·b. Возвращает (coefs с intercept первым, R², n)."""
    n = len(y)
    if n < 30:
        return np.zeros(X.shape[1] + 1), 0.0, n
    Xb = np.column_stack([np.ones(n), X])
    coef, *_ = np.linalg.lstsq(Xb, y, rcond=None)
    yhat = Xb @ coef
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return coef, r2, n


def _walk_forward(y: np.ndarray, X: np.ndarray, train_frac: float = 0.7) -> dict:
    """Fit на первых train_frac дней, оцениваем на остатке — честный out-of-sample R²
    и accuracy направления (доля дней где знак предсказания совпал с фактом)."""
    n = len(y)
    if n < 40:
        return {"train_r2": None, "oos_r2": None, "oos_dir_acc": None, "n_train": 0, "n_test": 0}
    split = int(n * train_frac)
    y_tr, y_te = y[:split], y[split:]
    X_tr, X_te = X[:split], X[split:]
    coef, r2_train, _ = _fit(y_tr, X_tr)
    Xb_te = np.column_stack([np.ones(len(y_te)), X_te])
    yhat = Xb_te @ coef
    ss_res = float(np.sum((y_te - yhat) ** 2))
    ss_tot = float(np.sum((y_te - y_te.mean()) ** 2))
    r2_oos = 1 - ss_res / ss_tot if ss_tot > 0 else None
    dir_acc = float(np.mean(np.sign(yhat) == np.sign(y_te))) if len(y_te) > 0 else None
    return {"train_r2": r2_train, "oos_r2": r2_oos, "oos_dir_acc": dir_acc,
            "n_train": len(y_tr), "n_test": len(y_te)}


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
    for p in EXTERNAL_PREDICTORS:
        if p in all_returns:
            pred_dates[p] = sorted(all_returns[p].keys())

    imoex_returns = all_returns.get("IMOEX", {})

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
                row: list[float] = []
                complete = True
                # 1) внешние предикторы (last-before-d)
                for p in EXTERNAL_PREDICTORS:
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
                # 2) momentum-фичи (все < d_str)
                mom = _momentum_features(imoex_returns, d_str)
                if any(mom[f] is None for f in MOMENTUM_FEATURES):
                    continue
                for f in MOMENTUM_FEATURES:
                    row.append(mom[f])

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
            wf = _walk_forward(y, X)

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
            results[tgt] = {
                "intercept_pct": round(intercept * 100, 3),
                "r2_in": round(r2, 3),
                "n": n,
                "wf_r2": round(wf["oos_r2"], 3) if wf["oos_r2"] is not None else None,
                "wf_dir_acc": round(wf["oos_dir_acc"], 3) if wf["oos_dir_acc"] is not None else None,
                "wf_n_train": wf["n_train"], "wf_n_test": wf["n_test"],
                "coefs": coefs,
            }
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
    print(f"{'target':8s}  {'R²_in':>6s}  {'R²_oos':>7s}  {'dir%':>5s}  n_tr/n_te")
    for tgt, s in stats.items():
        wf_r2 = f"{s['wf_r2']:+.3f}" if s.get('wf_r2') is not None else "  n/a"
        wf_da = f"{s['wf_dir_acc']*100:5.1f}" if s.get('wf_dir_acc') is not None else " n/a"
        print(f"{tgt:8s}  {s['r2_in']:+.3f}  {wf_r2}  {wf_da}  {s['wf_n_train']}/{s['wf_n_test']}")
    print()
    print("Ненулевые коэффициенты по фактору (после all-data fit):")
    for tgt, s in stats.items():
        big = {p: c for p, c in s["coefs"].items() if abs(c) > 0.02}
        if big:
            print(f"  {tgt}: " + ", ".join(f"{p}={c:+.2f}" for p, c in big.items()))


if __name__ == "__main__":
    main()
