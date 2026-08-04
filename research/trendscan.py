# -*- coding: utf-8 -*-
"""Шаг 1: найти в истории Bybit САМЫЙ трендовый отрезок — тот, где грид обязан ломаться.

Качаем 1h за ~2 года по всей корзине, считаем трендовость скользящим окном
(окно 1h-баров = 250 = ~10 суток, столько же, сколько 1000 баров 15m),
выбираем окна с максимальной трендовостью. Результат кладём в trend.pkl.
"""
import asyncio, os, pickle, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import httpx
from app.config import get_settings
from app.models import Candle

SCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(SCR, exist_ok=True)
CACHE = os.path.join(SCR, "hourly.pkl")
s = get_settings()
TOTAL_H = 17520          # ~2 года часовых баров


async def fetch_paged(c, sym, interval, total, end=None):
    out = []
    while len(out) < total:
        p = {"category": "linear", "symbol": sym, "interval": interval, "limit": 1000}
        if end:
            p["end"] = end
        for _ in range(3):
            try:
                r = await c.get(f"{s.active_base_url}/v5/market/kline", params=p)
                rows = r.json().get("result", {}).get("list", [])
                break
            except Exception:
                await asyncio.sleep(1.0)
                rows = []
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
    cm = {}
    async with httpx.AsyncClient(timeout=30) as c:
        for sym in s.symbols:
            cm[sym] = await fetch_paged(c, sym, "60", TOTAL_H)
            print(f"  {sym}: {len(cm[sym])} часовых баров", flush=True)
    with open(CACHE, "wb") as f:
        pickle.dump(cm, f)
    return cm


def trendiness(cw):
    vals = []
    for cs in cw.values():
        path = sum(abs(cs[i].c - cs[i - 1].c) for i in range(1, len(cs)))
        net = abs(cs[-1].c - cs[0].c)
        if path > 0:
            vals.append(net / path)
    return sum(vals) / len(vals) if vals else 0.0


def drift(cw):
    vals = [(cs[-1].c - cs[0].c) / cs[0].c * 100.0 for cs in cw.values() if cs[0].c]
    return sum(vals) / len(vals) if vals else 0.0


async def main():
    cm = await load()
    # общий отрезок по времени: обрезаем по минимальной длине
    n = min(len(v) for v in cm.values())
    cm = {k: v[-n:] for k, v in cm.items()}
    print(f"\nЗагружено {n} часовых баров ({n/24:.0f} суток)\n")

    WIN, STEP = 250, 50
    rows = []
    for a in range(0, n - WIN + 1, STEP):
        cw = {k: v[a:a + WIN] for k, v in cm.items()}
        rows.append((a, trendiness(cw), drift(cw), cw[s.symbols[0]][0].ts, cw[s.symbols[0]][-1].ts))

    rows.sort(key=lambda r: -r[1])
    import datetime as dt
    print(f"{'откуда':>8} {'трендовость':>12} {'дрейф корзины':>15}  период")
    print("-" * 80)
    for a, t, d, t0, t1 in rows[:12]:
        f = lambda ms: dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        print(f"{a:>8} {t:>12.3f} {d:>14.1f}%  {f(t0)} .. {f(t1)}")
    print("\nСамые СПОКОЙНЫЕ для контраста:")
    for a, t, d, t0, t1 in rows[-3:]:
        f = lambda ms: dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        print(f"{a:>8} {t:>12.3f} {d:>14.1f}%  {f(t0)} .. {f(t1)}")

    with open(os.path.join(SCR, "trendrows.pkl"), "wb") as f:
        pickle.dump(rows, f)


asyncio.run(main())
