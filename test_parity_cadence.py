# -*- coding: utf-8 -*-
"""Блок D: единый каденс + паритет Live=бэктест + честный trade-through в обоих путях.
Проверяет, что прежнее расхождение (бэктест перецентровал каждый бар, Live ждал)
устранено: на ОДНИХ данных бэктест-путь (step/run) и Live-путь (on_tick по тикам)
дают ИДЕНТИЧНЫЙ результат. Заявка ждёт на уровне; эмулятор подключён к обоим путям.
Запуск: pytest -q  (или python test_parity_cadence.py)
"""
import math
import random

from app.models import Candle, GridParams
from app.engine.paper import PaperEngine, _OpenOrder
from app.engine.costmodel import CostModel
from app.config import Settings


def synth_candles(n=400, p0=66000.0, seed=5):
    rnd = random.Random(seed)
    out, p = [], p0
    ts = 1_700_000_000_000
    for i in range(n):
        drift = math.sin(i / 20) * 40
        p = max(1000.0, p + drift + rnd.uniform(-150, 150))
        o = p
        h = o + abs(rnd.uniform(0, 110))
        l = o - abs(rnd.uniform(0, 110))
        c = l + rnd.random() * (h - l)
        out.append(Candle(ts=ts + i * 900_000, o=o, h=h, l=l, c=c, v=rnd.uniform(50, 300)))
        p = c
    return out


def make_engine(mode="avellaneda"):
    s = Settings()
    cost = CostModel.from_settings(s)
    p = GridParams(mode=mode, max_orders=8, order_usd=80.0)
    return PaperEngine("BTCUSDT", 1000.0, p, cost, {})


def _feed_live(eng, bars):
    """Live-путь через публичный on_tick: бар раскладывается на те же внутрибарные тики,
    что использует step() — но входная точка другая (как в реальной live-сессии)."""
    for bar in bars:
        eng.ind.update(bar)
        if not eng.ind.ready:
            eng._last_price = bar.c
            eng.equity_curve.append(eng.cash + eng.pos.unrealized(bar.c))
            continue
        if not eng.active and eng.p.mode != "manual":
            eng.active = True
        for px in eng._bar_ticks(bar):
            eng.on_tick(px, bar.ts)
        eng._last_price = bar.c
        eng.equity_curve.append(eng.cash + eng.pos.unrealized(bar.c))


def test_backtest_live_parity():
    """ГЛАВНЫЙ тест блока D: бэктест-путь (run/step) == Live-путь (on_tick) на одних данных."""
    for mode in ("avellaneda", "heuristic"):
        cs = synth_candles()
        W = 200
        bt = make_engine(mode)
        lv = make_engine(mode)
        for c in cs[:W]:                 # одинаковый прогрев (без торговли)
            bt.ind.update(c)
            lv.ind.update(c)
        bt._last_price = lv._last_price = cs[W - 1].c
        bt.run(cs[W:])                   # бэктест-путь
        _feed_live(lv, cs[W:])           # Live-путь

        assert bt.pos.qty == lv.pos.qty, f"[{mode}] позиция разошлась: {bt.pos.qty} != {lv.pos.qty}"
        assert bt.pos.realized == lv.pos.realized, f"[{mode}] realized разошёлся"
        assert bt.trades == lv.trades, f"[{mode}] число сделок разошлось: {bt.trades} != {lv.trades}"
        assert bt.cash == lv.cash, f"[{mode}] cash разошёлся"
        assert bt.buy_filled == lv.buy_filled and bt.sell_filled == lv.sell_filled
        fb = [(f.side, round(f.price, 6), round(f.size, 8)) for f in bt.fills]
        fl = [(f.side, round(f.price, 6), round(f.size, 8)) for f in lv.fills]
        assert fb == fl, f"[{mode}] филлы разошлись: {len(fb)} vs {len(fl)}"
        if mode == "avellaneda":
            assert bt.trades > 0, "контроль: на A-S сделки должны происходить (паритет нетривиален)"


def test_orders_rest_no_recenter():
    """Заявка ЖДЁТ на уровне: при малом движении (в пределах порога) сетка НЕ перецентруется."""
    cs = synth_candles(300)
    eng = make_engine("avellaneda")
    for c in cs[:-6]:
        eng.ind.update(c)
    center = cs[-7].c
    eng._last_price = center
    eng.start_strategy(center)
    g1 = sorted((o.side, round(o.price, 4)) for o in eng.orders if not o.manual)
    for i in range(5):                   # микродвижения << половины шага, без касания
        eng.on_tick(center * (1 + 1e-5 * (i - 2)), cs[-6 + i].ts)
    g2 = sorted((o.side, round(o.price, 4)) for o in eng.orders if not o.manual)
    assert g1 == g2, "сетка перецентровалась без исполнения (каденс нарушен)"


def test_requote_after_fill():
    """После исполнения заявки сетка переставляется по A-S (замысел «исполнилась — ставлю следующую»)."""
    cs = synth_candles(300)
    eng = make_engine("avellaneda")
    for c in cs[:-2]:
        eng.ind.update(c)
    center = cs[-3].c
    eng._last_price = center
    eng.start_strategy(center)
    buys = [o.price for o in eng.orders if o.side == "buy" and not o.manual]
    assert buys
    eng.on_tick(min(buys) * 0.999, cs[-2].ts)   # цена прошла через buy -> филл -> переоценка
    assert eng.pos.qty > 0, "buy должен был исполниться"
    assert any(not o.manual for o in eng.orders), "сетка должна быть переставлена после филла"


def test_emulator_connected_both_paths():
    """Честный trade-through подключён к обоим путям: process_trade исполняет по реальной
    сделке нужной стороны через уровень и НЕ исполняет встречную."""
    cs = synth_candles(300)
    eng = make_engine("avellaneda")
    for c in cs:
        eng.ind.update(c)
    px = cs[-1].c
    eng._last_price = px
    eng.active = True
    eng.quoter.last_step = 0.0                    # порог переоценки -> по ATR
    lvl = px - 0.3 * eng.ind.atr                  # в пределах порога (нет переоценки)
    qty = eng._order_qty(px)
    eng.orders = [_OpenOrder("buy", lvl, qty, 1, queue_ahead=0.0)]
    eng._quote_anchor = (px, eng.ind.sigma, 0.0)
    eng.process_trade(lvl, 999.0, "Buy", cs[-1].ts)    # не та сторона -> не исполняет
    assert eng.pos.qty == 0
    eng.process_trade(lvl, 999.0, "Sell", cs[-1].ts)   # продажа через уровень -> исполняет
    assert eng.pos.qty > 0


def test_process_trade_queue_melts():
    """В Live-пути очередь тает по реальному объёму (честная граница)."""
    cs = synth_candles(300)
    eng = make_engine("avellaneda")
    for c in cs:
        eng.ind.update(c)
    px = cs[-1].c
    eng._last_price = px
    eng.active = True
    eng.quoter.last_step = 0.0
    lvl = px - 0.3 * eng.ind.atr
    eng.orders = [_OpenOrder("buy", lvl, 1.0, 1, queue_ahead=2.0)]
    eng._quote_anchor = (px, eng.ind.sigma, 0.0)
    eng.process_trade(lvl, 1.0, "Sell", cs[-1].ts)     # тает очередь 2->1, нам ничего
    assert eng.pos.qty == 0
    eng.process_trade(lvl, 5.0, "Sell", cs[-1].ts)     # очередь исчерпана -> исполнение
    assert eng.pos.qty > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (parity / cadence)")
