# -*- coding: utf-8 -*-
"""Шаг 2: прогон сетки на ТРЕНДОВЫХ отрезках + база сравнения buy-and-hold.

Отрезки выбраны сканом истории (trendscan.py) как самые направленные за 2 года.
Качаем для них 15m (тот же таймфрейм, что и в исходных шести окнах), гоняем
дефолтную сетку и рядом — «купил корзину и держал» на те же деньги.

Ликвидация НЕ отключается: на тренде это и есть главный механизм убытка.
"""
import asyncio, os, pickle, sys, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import httpx
from app.config import get_settings
from app.models import Candle, GridParams
from app.engine.costmodel import CostModel
from app.engine.paper import PaperEngine
from app.portfolio.manager import risk_parity_alloc
from app.marketdata.bybit import fetch_many_meta

SCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(SCR, exist_ok=True)
CACHE = os.path.join(SCR, "trend15m.pkl")
s = get_settings()
BARS = 1000            # столько же, сколько в исходных шести окнах

# (подпись, дата КОНЦА отрезка UTC)
PERIODS = [
    ("обвал -31%",  "2026-02-06"),
    ("обвал -19%",  "2025-06-21"),
    ("обвал -21%",  "2026-06-06"),
    ("рост  +38%",  "2024-11-13"),
    ("рост  +23%",  "2025-07-16"),
    ("рост  +14%",  "2026-01-05"),
]


async def fetch_until(c, sym, end_ms, total=BARS):
    out, end = [], end_ms
    while len(out) < total:
        p = {"category": "linear", "symbol": sym, "interval": "15",
             "limit": 1000, "end": end}
        rows = []
        for _ in range(3):
            try:
                r = await c.get(f"{s.active_base_url}/v5/market/kline", params=p)
                rows = r.json().get("result", {}).get("list", [])
                break
            except Exception:
                await asyncio.sleep(1.0)
        if not rows:
            break
        batch = [Candle(ts=int(x[0]), o=float(x[1]), h=float(x[2]),
                        l=float(x[3]), c=float(x[4]), v=float(x[5])) for x in rows]
        batch.sort(key=lambda z: z.ts)
        out = batch + out
        end = batch[0].ts - 1
    out.sort(key=lambda z: z.ts)
    return out[-total:]


async def load():
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    res = {}
    async with httpx.AsyncClient(timeout=30) as c:
        for label, day in PERIODS:
            end_ms = int(dt.datetime.strptime(day, "%Y-%m-%d")
                         .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            cm = {}
            for sym in s.symbols:
                cs = await fetch_until(c, sym, end_ms)
                if len(cs) >= BARS * 0.9:
                    cm[sym] = cs
            res[label] = cm
            print(f"  {label}: {len(cm)} инструментов", flush=True)
    with open(CACHE, "wb") as f:
        pickle.dump(res, f)
    return res


def trendiness(cw):
    vals = []
    for cs in cw.values():
        path = sum(abs(cs[i].c - cs[i - 1].c) for i in range(1, len(cs)))
        net = abs(cs[-1].c - cs[0].c)
        if path > 0:
            vals.append(net / path)
    return sum(vals) / len(vals) if vals else 0.0


def run_grid(cm, params, specs, cap=1000.0):
    cost = CostModel.from_settings(s)
    allocs = risk_parity_alloc(cm, cap)
    eq = trades = 0.0, 0
    eq, trades, liq, blocked = 0.0, 0, 0, 0
    for sym, cs in cm.items():
        e = PaperEngine(sym, allocs[sym], params.model_copy(), cost, None,
                        spec=specs.get(sym))
        e.run(cs)
        sm = e.summary()
        eq += sm["equity"]
        trades += sm["trades"]
        liq += 1 if e.liquidated else 0
        blocked += 1 if e.blocked_reason else 0
    return (eq - cap) / cap * 100.0, trades, liq, blocked


def buy_and_hold(cm, cap=1000.0):
    """Купил корзину по тем же весам в первом баре, держал до последнего.
    Единственная издержка — тейкер на вход и на выход."""
    allocs = risk_parity_alloc(cm, cap)
    fee = s.taker_fee_bps / 10000.0
    end = 0.0
    for sym, cs in cm.items():
        a = allocs[sym]
        qty = a * (1 - fee) / cs[0].c
        end += qty * cs[-1].c * (1 - fee)
    return (end - cap) / cap * 100.0


async def main():
    data = await load()
    specs = await fetch_many_meta(s.symbols)
    P = GridParams()
    print(f"\nДефолт: mode={P.mode} step={P.grid_step_mode} "
          f"{P.grid_step_pct}% levels={P.grid_levels} side={P.grid_side}\n")

    print("=" * 96)
    print(f"{'отрезок':>14} {'трендовость':>12} {'СЕТКА':>10} {'BUY&HOLD':>10} "
          f"{'разница':>10} {'сделок':>8} {'ликв':>5} {'блок':>5}")
    print("=" * 96)
    tot_g = tot_b = 0.0
    wins = 0
    for label, cm in data.items():
        if not cm:
            continue
        t = trendiness(cm)
        g, tr, liq, bl = run_grid(cm, P, specs)
        b = buy_and_hold(cm)
        tot_g += g; tot_b += b; wins += g > 0
        print(f"{label:>14} {t:>12.3f} {g:>9.2f}% {b:>9.2f}% "
              f"{g-b:>9.2f}% {tr:>8} {liq:>5} {bl:>5}")
    print("-" * 96)
    n = len([1 for cm in data.values() if cm])
    print(f"{'СРЕДНЕЕ':>14} {'':>12} {tot_g/n:>9.2f}% {tot_b/n:>9.2f}% "
          f"{(tot_g-tot_b)/n:>9.2f}%")
    print(f"\nПрибыльных трендовых окон: {wins} из {n}")


asyncio.run(main())
