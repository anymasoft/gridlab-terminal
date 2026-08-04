# -*- coding: utf-8 -*-
"""Шаг 3: walk-forward — параметры подбираются на одном отрезке, проверяются на СЛЕДУЮЩЕМ.

Отвечает на пункт 1.1 аудита: насколько +8.78% завышены тем, что параметры
выбирались на тех же данных, где потом отчитывались.

Схема: обучение 1000 баров -> проверка 500 баров -> сдвиг на 500.
На каждом фолде сравниваем три вещи на ОДНОМ И ТОМ ЖЕ проверочном отрезке:
  1. лучшие параметры обучения      (честная оценка «как будет»)
  2. дефолтные параметры            (то, что зашито в продукт)
  3. лучшие параметры САМОГО проверочного отрезка (недостижимый потолок)
Разрыв между 1 и 3 — и есть цена подгонки.

Плюс buy-and-hold на тех же проверочных отрезках (пункт 1.3).
"""
import os, pickle, sys
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

from app.config import get_settings
from app.models import GridParams
from app.engine.costmodel import CostModel
from app.engine.paper import PaperEngine
from app.portfolio.manager import risk_parity_alloc

SCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(SCR, exist_ok=True)
s = get_settings()
IS, OOS = 1000, 500

GRID = [
    {"grid_step_pct": p, "grid_levels": n, "grid_side": sd}
    for p in (0.5, 0.75, 1.0, 1.5, 2.0)
    for n in (5, 10, 15)
    for sd in ("neutral", "long")
]

_D = {}


def _init(cm_bytes, specs):
    _D["cm"] = pickle.loads(cm_bytes)
    _D["specs"] = specs


def _run(args):
    a, b, ci = args
    combo = GRID[ci]
    cm = {k: v[a:b] for k, v in _D["cm"].items()}
    P = GridParams(**{**GridParams().model_dump(), **combo})
    cost = CostModel.from_settings(s)
    allocs = risk_parity_alloc(cm, 1000.0)
    eq = 0.0
    for sym, cs in cm.items():
        e = PaperEngine(sym, allocs[sym], P.model_copy(), cost, None,
                        spec=_D["specs"].get(sym))
        e.run(cs)
        eq += e.summary()["equity"]
    return (a, b, ci, (eq - 1000.0) / 10.0)


def buy_and_hold(cm, a, b):
    seg = {k: v[a:b] for k, v in cm.items()}
    allocs = risk_parity_alloc(seg, 1000.0)
    fee = s.taker_fee_bps / 10000.0
    end = 0.0
    for sym, cs in seg.items():
        end += allocs[sym] * (1 - fee) / cs[0].c * cs[-1].c * (1 - fee)
    return (end - 1000.0) / 10.0


def main():
    lp = os.path.join(SCR, "long.pkl")
    if not os.path.exists(lp):
        raise SystemExit(f"нет {lp} — сначала запустите research/trendrun.py, "
                         "он качает и кэширует историю")
    with open(lp, "rb") as f:
        cm = pickle.load(f)

    sp = os.path.join(SCR, "specs.pkl")
    if os.path.exists(sp):
        with open(sp, "rb") as f:
            specs = pickle.load(f)
    else:
        import asyncio
        from app.marketdata.bybit import fetch_many_meta
        specs = asyncio.run(fetch_many_meta(s.symbols))
        with open(sp, "wb") as f:
            pickle.dump(specs, f)
    n = min(len(v) for v in cm.values())
    cm = {k: v[-n:] for k, v in cm.items()}

    folds = []
    a = 0
    while a + IS + OOS <= n:
        folds.append((a, a + IS, a + IS + OOS))
        a += OOS
    print(f"{n} баров, {len(folds)} фолдов, {len(GRID)} комбинаций\n", flush=True)

    jobs = []
    for i0, i1, i2 in folds:
        for ci in range(len(GRID)):
            jobs.append((i0, i1, ci))      # обучение
            jobs.append((i1, i2, ci))      # проверка
    cmb = pickle.dumps(cm)

    res = {}
    with ProcessPoolExecutor(initializer=_init, initargs=(cmb, specs)) as ex:
        done = 0
        for a, b, ci, roi in ex.map(_run, jobs, chunksize=4):
            res[(a, b, ci)] = roi
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)

    dflt = GridParams().model_dump()
    di = next(i for i, c in enumerate(GRID)
              if all(abs(dflt[k] - v) < 1e-9 if isinstance(v, float) else dflt[k] == v
                     for k, v in c.items()))

    print("\n" + "=" * 104)
    print(f"{'фолд':>5} {'параметры обучения':>26} {'на обучении':>12} "
          f"{'ЧЕСТНО':>9} {'дефолт':>9} {'потолок':>9} {'buy&hold':>10}")
    print("=" * 104)
    t_hon = t_def = t_ceil = t_bh = 0.0
    w_hon = w_def = 0
    for k, (i0, i1, i2) in enumerate(folds):
        best_is = max(range(len(GRID)), key=lambda c: res[(i0, i1, c)])
        hon = res[(i1, i2, best_is)]
        deff = res[(i1, i2, di)]
        ceil_i = max(range(len(GRID)), key=lambda c: res[(i1, i2, c)])
        ceil = res[(i1, i2, ceil_i)]
        bh = buy_and_hold(cm, i1, i2)
        c = GRID[best_is]
        lbl = f"{c['grid_step_pct']}% x{c['grid_levels']} {c['grid_side'][:4]}"
        t_hon += hon; t_def += deff; t_ceil += ceil; t_bh += bh
        w_hon += hon > 0; w_def += deff > 0
        print(f"{k+1:>5} {lbl:>26} {res[(i0,i1,best_is)]:>11.2f}% "
              f"{hon:>8.2f}% {deff:>8.2f}% {ceil:>8.2f}% {bh:>9.2f}%")
    m = len(folds)
    print("-" * 104)
    print(f"{'СРЕДНЕЕ':>5} {'':>26} {'':>12} {t_hon/m:>8.2f}% {t_def/m:>8.2f}% "
          f"{t_ceil/m:>8.2f}% {t_bh/m:>9.2f}%")
    print(f"\nПрибыльных проверочных отрезков: честный подбор {w_hon}/{m}, "
          f"дефолт {w_def}/{m}")
    print(f"Цена подгонки (потолок минус честно): {(t_ceil-t_hon)/m:.2f}% за отрезок")

    # какие параметры выигрывали на обучении — стабильны ли они?
    from collections import Counter
    cnt = Counter()
    for i0, i1, i2 in folds:
        b = max(range(len(GRID)), key=lambda c: res[(i0, i1, c)])
        cnt[(GRID[b]['grid_step_pct'], GRID[b]['grid_levels'], GRID[b]['grid_side'])] += 1
    print("\nЧастота победителей на обучении (нестабильность = подгонка под шум):")
    for k, v in cnt.most_common():
        print(f"   {k[0]}% x{k[1]} {k[2]:>7}: {v}")


if __name__ == "__main__":
    main()
