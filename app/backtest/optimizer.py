"""Parameter Optimizer — grid search с multiprocessing и real-time прогрессом.

Режимы: full (полный перебор), around (вокруг текущих), random, genetic (заглушка).
Расширенные метрики: Sortino, avg_trade. Robustness score через анализ соседей.
"""
from __future__ import annotations

import itertools
import math
import random as _random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from typing import Any

from ..engine.costmodel import CostModel
from ..engine.paper import PaperEngine
from ..models import Candle, GridParams

INT_KEYS = {"levels", "ema", "max_orders", "atr_window", "as_horizon"}


def _apply(base: GridParams, combo: dict) -> GridParams:
    d = base.model_dump()
    for k, v in combo.items():
        d[k] = int(round(v)) if k in INT_KEYS else v
    if "max_orders" in combo:
        d["levels"] = d["max_orders"]
    return GridParams(**d)


# ───────────────── метрики ─────────────────

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


def _sortino(curve: list[float]) -> float:
    if len(curve) < 3:
        return 0.0
    rets = [(curve[i] - curve[i - 1]) / curve[i - 1] for i in range(1, len(curve)) if curve[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    down = [r for r in rets if r < 0]
    if not down:
        return 10.0 if m > 0 else 0.0
    dd_var = sum(r ** 2 for r in down) / len(down)
    dd_sd = math.sqrt(dd_var)
    return (m / dd_sd) if dd_sd > 1e-12 else 0.0


def _run_one(candles: list[Candle], params: GridParams, capital: float,
             cost: CostModel, spot: bool) -> dict:
    c = cost
    if spot:
        c = replace(cost, default_funding_rate=0.0)
    eng = PaperEngine("OPT", capital, params, c, funding={} if spot else None)
    eng._check_liquidation = lambda ts, mark: False
    eng.active = True
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
    avg_trade = (eng.pos.realized / eng.trades) if eng.trades > 0 else 0.0
    return {
        "roi": round(roi, 3),
        "final_equity": round(final_eq, 2),
        "max_dd": round(dd, 3),
        "trades": eng.trades,
        "fees": round(eng.pos.fees_paid, 2),
        "fees_pct": round(fees_pct, 3),
        "profit_factor": round(pf, 3),
        "sharpe": round(_sharpe(curve), 4),
        "sortino": round(_sortino(curve), 4),
        "avg_trade": round(avg_trade, 4),
        "liquidated": eng.liquidated,
    }


# ───────────────── скоринг ─────────────────

SCORING_PRESETS = {
    "max_profit": lambda m: m["roi"],
    "min_dd": lambda m: -m["max_dd"],
    "profit_dd": lambda m: (m["roi"] / max(m["max_dd"], 0.01)),
    "max_sharpe": lambda m: m["sharpe"],
    "robust": lambda m: m["roi"] - 0.5 * m["max_dd"] - 0.2 * m["fees_pct"] + m["sharpe"] * 10,
}


def score_result(m: dict, preset: str = "robust") -> float:
    fn = SCORING_PRESETS.get(preset, SCORING_PRESETS["robust"])
    return round(fn(m), 4)


# ───────────────── генерация комбинаций ─────────────────

def _gen_full(ranges: dict[str, list]) -> list[dict]:
    keys = list(ranges.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*[ranges[k] for k in keys])]


def _gen_around(current: dict, ranges: dict[str, list], steps: int = 2) -> list[dict]:
    expanded = {}
    for k, vals in ranges.items():
        cur = current.get(k, vals[len(vals) // 2])
        vals_sorted = sorted(vals)
        try:
            idx = min(range(len(vals_sorted)), key=lambda i: abs(vals_sorted[i] - cur))
        except ValueError:
            idx = 0
        lo = max(0, idx - steps)
        hi = min(len(vals_sorted), idx + steps + 1)
        expanded[k] = vals_sorted[lo:hi]
    return _gen_full(expanded)


def _gen_random(ranges: dict[str, list], n: int = 500) -> list[dict]:
    keys = list(ranges.keys())
    combos = []
    for _ in range(n):
        combo = {k: _random.choice(ranges[k]) for k in keys}
        combos.append(combo)
    return combos


def generate_combos(mode: str, ranges: dict[str, list],
                    current: dict | None = None, n_random: int = 500) -> list[dict]:
    if mode == "around":
        return _gen_around(current or {}, ranges)
    if mode == "random":
        return _gen_random(ranges, n_random)
    return _gen_full(ranges)


# ───────────────── воркер для multiprocessing ─────────────────

_WORKER_DATA: dict[str, Any] = {}


def _init_worker(candles_data: list, capital: float, cost_dict: dict, spot: bool):
    _WORKER_DATA["candles"] = [Candle(*c) for c in candles_data]
    _WORKER_DATA["capital"] = capital
    _WORKER_DATA["cost"] = CostModel(**cost_dict)
    _WORKER_DATA["spot"] = spot


def _run_worker(args: tuple) -> dict:
    idx, combo, base_dict = args
    try:
        base = GridParams(**base_dict)
        params = _apply(base, combo)
        m = _run_one(_WORKER_DATA["candles"], params, _WORKER_DATA["capital"],
                     _WORKER_DATA["cost"], _WORKER_DATA["spot"])
        return {"idx": idx, "combo": combo, "metrics": m}
    except Exception as e:
        return {"idx": idx, "combo": combo, "metrics": {
            "roi": 0, "final_equity": 0, "max_dd": 0, "trades": 0,
            "fees": 0, "fees_pct": 0, "profit_factor": 0, "sharpe": 0,
            "sortino": 0, "avg_trade": 0, "liquidated": False,
            "_error": str(e),
        }}


def run_optimizer_sync(candles: list[Candle], combos: list[dict], base: GridParams,
                       capital: float, cost: CostModel, spot: bool,
                       preset: str = "robust",
                       on_progress=None) -> list[dict]:
    """Синхронный запуск оптимизатора с multiprocessing. on_progress(i, n, result) для стриминга."""
    import os
    workers = max(1, os.cpu_count() - 1)
    candles_data = [(c.ts, c.o, c.h, c.l, c.c, c.v) for c in candles]
    cost_dict = {
        "maker_fee": cost.maker_fee, "taker_fee": cost.taker_fee,
        "slippage": cost.slippage, "leverage": cost.leverage,
        "maint_margin": cost.maint_margin, "default_funding_rate": cost.default_funding_rate,
        "funding_interval_ms": cost.funding_interval_ms,
    }
    base_dict = base.model_dump()
    tasks = [(i, combo, base_dict) for i, combo in enumerate(combos)]
    results = []

    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_init_worker,
                             initargs=(candles_data, capital, cost_dict, spot)) as pool:
        for result in pool.map(_run_worker, tasks, chunksize=max(1, len(tasks) // (workers * 4))):
            result["score"] = score_result(result["metrics"], preset)
            results.append(result)
            if on_progress:
                on_progress(len(results), len(combos), result)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ───────────────── robustness (доля прибыльных соседей) ─────────────────

def robustness_analysis(results: list[dict], ranges: dict[str, list], top_n: int = 10) -> list[dict]:
    keys = list(ranges.keys())
    lookup = {}
    for r in results:
        k = tuple(r["combo"].get(k, 0) for k in keys)
        lookup[k] = r

    enriched = []
    for r in results[:top_n]:
        total = 0
        profitable = 0
        for ki, k in enumerate(keys):
            vals = sorted(ranges[k])
            cur = r["combo"].get(k, 0)
            try:
                idx = vals.index(cur)
            except ValueError:
                idx = min(range(len(vals)), key=lambda i: abs(vals[i] - cur))
            for delta in [-1, 1]:
                ni = idx + delta
                if 0 <= ni < len(vals):
                    neighbor_combo = dict(r["combo"])
                    neighbor_combo[k] = vals[ni]
                    nk = tuple(neighbor_combo.get(kk, 0) for kk in keys)
                    if nk in lookup:
                        total += 1
                        if lookup[nk]["metrics"]["roi"] > 0:
                            profitable += 1
        entry = dict(r)
        entry["robustness"] = {
            "neighbor_total": total,
            "neighbor_profitable": profitable,
            "neighbor_share": round(profitable / total, 3) if total > 0 else 0.0,
        }
        enriched.append(entry)
    return enriched
