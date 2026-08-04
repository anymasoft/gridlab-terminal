"""Backtest Lab — 4 режима исследования на ЕДИНОМ Quoter (тот же код, что в Live):
single / sweep / heatmap / walk-forward.

Ключевая дисциплина против оверфита:
- ранжируем НЕ по голой прибыли, а по робастной метрике (profit − 0.5·DD − 0.2·fees);
- меряем ШИРИНУ прибыльной зоны (доля прибыльных соседей вокруг оптимума), а не одну точку;
- walk-forward: оптимизация на окне → проверка на следующем (out-of-sample).

ETHBTC — спот → funding выключен (cost.default_funding_rate=0, funding={}).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import replace

from ..analytics import metrics as _M
from ..engine.costmodel import CostModel
from ..engine.paper import PaperEngine
from ..models import Candle, GridParams


# ───────────────────────── один прогон ─────────────────────────
def _max_drawdown_pct(curve: list[float]) -> float:
    peak, dd = (curve[0] if curve else 0.0), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100.0


def _sharpe(curve: list[float]) -> float:
    if len(curve) < 3:
        return 0.0
    rets = [(curve[i] - curve[i - 1]) / curve[i - 1] for i in range(1, len(curve)) if curve[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return (m / sd) if sd > 1e-12 else 0.0


def _downsample(curve: list[float], n: int = 240) -> list[float]:
    if len(curve) <= n:
        return [round(v, 2) for v in curve]
    step = len(curve) / n
    return [round(curve[int(i * step)], 2) for i in range(n)]


def run_backtest(candles: list[Candle], params: GridParams, capital: float,
                 cost: CostModel, spot: bool = True) -> dict:
    """Один бэктест. Возвращает метрики + эквити-кривую."""
    c = cost
    if spot:
        c = replace(cost, default_funding_rate=0.0)   # спот → без funding
    eng = PaperEngine("ETHBTC", capital, params, c, funding={} if spot else None)
    eng.run(candles)
    curve = eng.equity_curve or [capital]
    final_eq = curve[-1]
    roi = (final_eq - capital) / capital * 100.0 if capital else 0.0
    dd = _max_drawdown_pct(curve)
    pnls = eng.realized_pnls
    gross_p = sum(x for x in pnls if x > 0)
    gross_l = -sum(x for x in pnls if x < 0)
    pf = (gross_p / gross_l) if gross_l > 1e-9 else (gross_p if gross_p > 0 else 0.0)
    fees_pct = eng.pos.fees_paid / capital * 100.0 if capital else 0.0
    wins = sum(1 for x in eng.realized_pnls if x > 0)
    lo, hi = _M.wilson_ci(wins, len(eng.realized_pnls))
    return {
        "roi": round(roi, 3),
        "final_equity": round(final_eq, 2),
        "max_dd": round(dd, 3),
        "trades": eng.trades,
        "fees": round(eng.pos.fees_paid, 2),
        "fees_pct": round(fees_pct, 3),
        "funding": round(eng.pos.funding_paid, 2),
        "realized": round(eng.pos.realized, 2),
        "profit_factor": round(pf, 3),
        "sharpe": round(_sharpe(curve), 4),
        "liquidated": eng.liquidated,
        "equity_curve": _downsample(curve),
        "bars": len(candles),
        # ── метрики маркет-мейкинга (блок F) ──
        "mm": eng.mm_metrics(),                          # разложение PnL спред/инвентарь + fill-rate + markout
        "inventory": _M.inventory_stats(eng.inv_curve),  # поведение инвентаря во времени
        "winrate_ci": [round(lo, 3), round(hi, 3)],       # Wilson-CI доли прибыльных round-trip
    }


# ───────────────────────── робастная метрика ─────────────────────────
DEFAULT_WEIGHTS = {"w_dd": 0.5, "w_fees": 0.2}


def robust_score(m: dict, w: dict | None = None) -> float:
    w = w or DEFAULT_WEIGHTS
    score = m["roi"] - w.get("w_dd", 0.5) * m["max_dd"] - w.get("w_fees", 0.2) * m["fees_pct"]
    if m.get("liquidated"):
        score -= 100.0   # ликвидация — дисквалификация
    return round(score, 3)


# ───────────────────────── sweep (перебор) ─────────────────────────
INT_KEYS = {"levels", "ema", "max_orders", "atr_window", "as_horizon"}
MAX_COMBOS = 2000


def _apply(base: GridParams, combo: dict) -> GridParams:
    d = base.model_dump()
    for k, v in combo.items():
        d[k] = int(round(v)) if k in INT_KEYS else v
    return GridParams(**d)


def sweep(candles: list[Candle], grid: dict[str, list], base: GridParams,
          capital: float, cost: CostModel, weights: dict | None = None,
          spot: bool = True) -> dict:
    """Полный перебор сетки параметров. Возвращает отсортированные по robust_score
    результаты + анализ ширины прибыльной зоны."""
    keys = list(grid.keys())
    value_lists = [grid[k] for k in keys]
    combos = list(itertools.product(*value_lists))
    truncated = False
    if len(combos) > MAX_COMBOS:
        combos = combos[:MAX_COMBOS]
        truncated = True

    results = []
    for vals in combos:
        combo = dict(zip(keys, vals))
        p = _apply(base, combo)
        m = run_backtest(candles, p, capital, cost, spot)
        m.pop("equity_curve", None)   # в таблице кривая не нужна
        results.append({"params": combo, "metrics": m, "score": robust_score(m, weights)})

    results.sort(key=lambda r: r["score"], reverse=True)
    profitable = [r for r in results if r["metrics"]["roi"] > 0]
    zone = {
        "total": len(results),
        "profitable": len(profitable),
        "profitable_share": round(len(profitable) / len(results), 3) if results else 0.0,
    }
    return {"keys": keys, "results": results, "zone": zone, "truncated": truncated,
            "max_combos": MAX_COMBOS}


# ───────────────────────── heatmap (2D) ─────────────────────────
def heatmap(candles: list[Candle], xkey: str, xvals: list, ykey: str, yvals: list,
            base: GridParams, capital: float, cost: CostModel, metric: str = "score",
            weights: dict | None = None, spot: bool = True) -> dict:
    """2D-карта: ось X=xkey, Y=ykey, значение = metric (score|roi|max_dd|trades).
    Плюс доля прибыльных соседей вокруг лучшей точки (ширина зоны)."""
    grid_vals = []   # [y][x]
    raw_roi = []
    for yv in yvals:
        row, roirow = [], []
        for xv in xvals:
            p = _apply(base, {xkey: xv, ykey: yv})
            m = run_backtest(candles, p, capital, cost, spot)
            m.pop("equity_curve", None)
            val = robust_score(m, weights) if metric == "score" else m.get(metric, 0.0)
            row.append(round(val, 3))
            roirow.append(m["roi"])
        grid_vals.append(row)
        raw_roi.append(roirow)

    # лучшая точка по score-матрице и ширина прибыльной зоны вокруг неё
    best = {"x": None, "y": None, "val": -1e18}
    for j, yv in enumerate(yvals):
        for i, xv in enumerate(xvals):
            if grid_vals[j][i] > best["val"]:
                best = {"x": xv, "y": yv, "i": i, "j": j, "val": grid_vals[j][i]}
    neigh_total = neigh_prof = 0
    if best["x"] is not None:
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                jj, ii = best["j"] + dj, best["i"] + di
                if 0 <= jj < len(yvals) and 0 <= ii < len(xvals):
                    neigh_total += 1
                    if raw_roi[jj][ii] > 0:
                        neigh_prof += 1
    return {
        "xkey": xkey, "ykey": ykey, "xvals": xvals, "yvals": yvals,
        "metric": metric, "grid": grid_vals, "roi": raw_roi,
        "best": best,
        "neighbor_profitable": neigh_prof, "neighbor_total": neigh_total,
        "neighbor_share": round(neigh_prof / neigh_total, 3) if neigh_total else 0.0,
    }


# ───────────────────────── walk-forward ─────────────────────────
def walk_forward(candles: list[Candle], grid: dict[str, list], base: GridParams,
                 capital: float, cost: CostModel, train_days: int = 30,
                 test_days: int = 7, weights: dict | None = None,
                 spot: bool = True, max_windows: int = 12) -> dict:
    """Оптимизация на train-окне → проверка на следующем test-окне (out-of-sample).
    Показывает, держатся ли параметры вне выборки (анти-оверфит)."""
    if not candles:
        return {"windows": [], "summary": {}}
    day_ms = 86_400_000
    t0, t1 = candles[0].ts, candles[-1].ts
    train_ms, test_ms = train_days * day_ms, test_days * day_ms

    windows = []
    start = t0
    while start + train_ms + test_ms <= t1 and len(windows) < max_windows:
        tr = [c for c in candles if start <= c.ts < start + train_ms]
        te = [c for c in candles if start + train_ms <= c.ts < start + train_ms + test_ms]
        if len(tr) > 50 and len(te) > 20:
            sw = sweep(tr, grid, base, capital, cost, weights, spot)
            best = sw["results"][0]
            p = _apply(base, best["params"])
            oos = run_backtest(te, p, capital, cost, spot)
            oos.pop("equity_curve", None)
            windows.append({
                "train_from": tr[0].ts, "train_to": tr[-1].ts,
                "test_from": te[0].ts, "test_to": te[-1].ts,
                "best_params": best["params"],
                "in_sample": {"roi": best["metrics"]["roi"], "score": best["score"],
                              "max_dd": best["metrics"]["max_dd"]},
                "out_sample": {"roi": oos["roi"], "score": robust_score(oos, weights),
                               "max_dd": oos["max_dd"], "trades": oos["trades"]},
            })
        start += test_ms   # сдвиг на тест-окно

    oos_rois = [w["out_sample"]["roi"] for w in windows]
    summary = {
        "windows": len(windows),
        "oos_avg_roi": round(sum(oos_rois) / len(oos_rois), 3) if oos_rois else 0.0,
        "oos_profitable": sum(1 for r in oos_rois if r > 0),
        "oos_profitable_share": round(sum(1 for r in oos_rois if r > 0) / len(oos_rois), 3) if oos_rois else 0.0,
        "train_days": train_days, "test_days": test_days,
    }
    return {"windows": windows, "summary": summary}
