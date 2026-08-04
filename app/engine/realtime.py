"""Realtime-сессия: живой grid-трейдинг на бумажном счёте по реальным тикам Bybit.

Одно ws-подключение к Bybit на корзину; на каждый тик соответствующий PaperEngine
исполняет пересечённые лимитки. Портфельный эквити агрегируется и шлётся в UI ~3/сек.
Это тестовая площадка живой стратегии, а не бэктест."""
from __future__ import annotations

import asyncio
import contextlib
import time

from ..config import Settings
from ..marketdata import bybit
from ..models import Candle, GridParams
from ..portfolio.manager import _action_color, _fmt_price, risk_parity_alloc
from .costmodel import CostModel
from .paper import PaperEngine

_INTERVAL_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "30": 1_800_000,
                "60": 3_600_000, "240": 14_400_000}


def _downsample(xs, n=22):
    if len(xs) <= n:
        return list(xs)
    step = len(xs) / n
    return [xs[min(int(i * step), len(xs) - 1)] for i in range(n)]


async def run_live(ws, settings: Settings, params: GridParams,
                   interval: str, selected: str, candles_cache, funding_cache):
    s = settings
    syms = s.symbols
    interval_ms = _INTERVAL_MS.get(str(interval), 900_000)

    # данные: свечи (история для прогрева + график) и funding
    cmap = candles_cache or {}
    if not cmap:
        loaded = await bybit.fetch_many_klines(syms, interval, s.history_bars)
        cmap = loaded
    fmap = funding_cache or {}
    for sym in syms:
        if sym not in fmap:
            fmap[sym] = await bybit.fetch_funding(sym)

    cmap = {k: v for k, v in cmap.items() if v}
    if not cmap:
        await ws.send_json({"type": "error", "msg": "нет свечей"})
        return
    syms = list(cmap.keys())
    if selected not in syms:
        selected = syms[0]

    cost = CostModel.from_settings(s)
    allocs = risk_parity_alloc(cmap, s.start_capital)
    engines: dict[str, PaperEngine] = {}
    hist: dict[str, list[Candle]] = {}
    for sym in syms:
        e = PaperEngine(sym, allocs[sym], params, cost, fmap.get(sym))
        e.warm(cmap[sym])
        e.last_funding_ts = cmap[sym][-1].ts
        # сетку НЕ выставляем автоматически — только по явному запуску стратегии
        engines[sym] = e
        hist[sym] = list(cmap[sym][-150:])

    state = {"selected": selected, "stop": False, "started": False}
    ob_state = {"sym": None, "data": None}

    # читатель сообщений клиента (смена инструмента / ручные ордера / стоп)
    async def reader():
        try:
            while True:
                msg = await ws.receive_json()
                act = msg.get("action")
                if act == "select" and msg.get("symbol") in engines:
                    state["selected"] = msg["symbol"]
                elif act == "start":
                    state["started"] = True
                    for en in engines.values():
                        if en._last_price > 0:
                            en.start_strategy(en._last_price)
                elif act == "stop_strategy":
                    state["started"] = False
                    for en in engines.values():
                        en.stop_strategy()
                elif act == "place_order":
                    e = engines.get(msg.get("symbol"))
                    if e and msg.get("price") and msg.get("qty"):
                        e.add_manual_order(msg.get("side", "buy"), float(msg["price"]), float(msg["qty"]))
                elif act == "cancel_order":
                    e = engines.get(msg.get("symbol"))
                    if e and msg.get("oid") is not None:
                        e.cancel_manual(int(msg["oid"]))
                elif act == "stop":
                    state["stop"] = True
                    return
        except Exception:
            state["stop"] = True

    # стакан Bybit по выбранному инструменту (REST, ~2/сек)
    async def ob_loop():
        while not state["stop"]:
            sym = state["selected"]
            try:
                ob_state["data"] = await bybit.fetch_orderbook(sym, 16, s)
                ob_state["sym"] = sym
            except Exception:
                pass
            await asyncio.sleep(0.5)

    reader_task = asyncio.create_task(reader())
    ob_task = asyncio.create_task(ob_loop())
    port_curve: list[float] = []       # equity (mark-to-market) = balance + нереализ.
    bal_curve: list[float] = []        # balance (только реализованное) — ступеньки
    last_send = 0.0

    def build_frame():
        total_eq = sum(e.equity() for e in engines.values())
        total_real = sum(e.pos.realized for e in engines.values())
        total_unreal = sum(e.pos.unrealized(e._last_price) for e in engines.values())
        total_fees = sum(e.pos.fees_paid for e in engines.values())
        total_fund = sum(e.pos.funding_paid for e in engines.values())
        open_ord = sum(len(e.orders) for e in engines.values())
        balance = sum(e.cash for e in engines.values())
        roi = (total_eq - s.start_capital) / s.start_capital * 100.0
        peak, dd = (port_curve[0] if port_curve else total_eq), 0.0
        for v in port_curve:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak)

        instruments = []
        for sym, e in engines.items():
            sm = e.summary()
            instruments.append({
                "sym": sym, "alloc": round(e.alloc, 2),
                "pnl": round(sm["pnl"], 2), "pnl_pct": round(sm["pnl_pct"], 2),
                "orders": len(e.orders), "status": "stop" if e.liquidated else "active",
                "trades": e.trades,
                "spark": _downsample(e.equity_curve_live[-120:] or [e.alloc, e.equity()]),
            })

        sel = state["selected"]
        se = engines[sel]
        manual = [{"oid": o.oid, "side": o.side, "price": o.price, "size": o.size}
                  for o in se.orders if o.manual]
        grid = [{"side": o.side, "price": o.price, "size": o.size, "level": o.level}
                for o in se.orders if not o.manual]
        ob = ob_state["data"] if ob_state["sym"] == sel else None
        tail = hist[sel]
        ts_arr = [c.ts for c in tail]
        markers = []
        for f in se.fills:
            if not f.is_maker:
                continue
            import bisect
            j = bisect.bisect_left(ts_arr, f.ts)
            j = min(max(j, 0), len(tail) - 1)
            markers.append({"i": j, "side": f.side, "price": f.price})

        # журнал — слить события всех движков, новые сверху
        raw = []
        for sym, e in engines.items():
            for ev in e.events[-30:]:
                raw.append((ev, sym))
        raw.sort(key=lambda x: x[0].ts, reverse=True)
        recent = []
        for ev, sym in raw[:60]:
            recent.append({
                "ts": ev.ts, "action": ev.action, "actionColor": _action_color(ev.action),
                "sym": sym, "price": _fmt_price(ev.price) if ev.price else "—",
                "size": (f"{ev.size:.4f}".rstrip("0").rstrip(".")) if ev.size else "—",
                "fee": f"-${ev.fee:.2f}" if ev.fee else "—",
                "pnl": (("+" if ev.pnl >= 0 else "-") + f"${abs(ev.pnl):.2f}") if ev.pnl else "—",
                "pnlColor": "#157a4f" if ev.pnl > 0 else ("#a93529" if ev.pnl < 0 else "#939a93"),
                "comment": ev.comment,
            })

        return {
            "type": "frame", "live": True, "selected": sel,
            "started": state["started"],
            "price": se._last_price,
            "dash": {"balance": round(balance, 2), "equity": round(total_eq, 2),
                     "roi": round(roi, 2), "drawdown": round(dd * 100, 2),
                     "realized": round(total_real, 2), "unrealized": round(total_unreal, 2),
                     "open_orders": open_ord, "fees": round(total_fees, 2),
                     "funding": round(total_fund, 2)},
            "instruments": instruments,
            "candles": [[c.o, c.h, c.l, c.c, c.v] for c in tail],
            "times": [int(c.ts // 1000) for c in tail],
            "markers": markers[-60:],
            "manual": manual,
            "grid": grid,
            "orderbook": ob,
            "equity_curve": [round(v, 2) for v in port_curve[-300:]],
            "balance_curve": [round(v, 2) for v in bal_curve[-300:]],
            "recent": recent,
        }

    try:
        await ws.send_json(build_frame())
        async for sym, price, tts in bybit.stream_tickers(syms, s):
            if state["stop"]:
                break
            e = engines.get(sym)
            if not e:
                continue
            cs = hist[sym]
            cur = cs[-1]
            # роллбар: новая свеча по интервалу
            if tts - cur.ts >= interval_ms:
                e.ind.update(cur)                      # закрыли свечу → обновили ATR/EMA
                cs.append(Candle(ts=cur.ts + interval_ms, o=price, h=price, l=price, c=price, v=0))
                if len(cs) > 150:
                    cs.pop(0)
                cur = cs[-1]
            else:
                cur.c = price
                cur.h = max(cur.h, price)
                cur.l = min(cur.l, price)
            e.on_tick(price, tts)
            e.equity_curve_live.append(e.equity())
            if len(e.equity_curve_live) > 400:
                e.equity_curve_live.pop(0)

            now = time.monotonic()
            if now - last_send >= 0.33:
                last_send = now
                total_eq = sum(en.equity() for en in engines.values())
                total_bal = sum(en.cash for en in engines.values())
                port_curve.append(round(total_eq, 2))
                bal_curve.append(round(total_bal, 2))
                if len(port_curve) > 600:
                    port_curve.pop(0); bal_curve.pop(0)
                await ws.send_json(build_frame())
    except Exception as ex:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "msg": str(ex)})
    finally:
        state["stop"] = True
        reader_task.cancel()
        ob_task.cancel()
        for t in (reader_task, ob_task):
            with contextlib.suppress(Exception):
                await t
