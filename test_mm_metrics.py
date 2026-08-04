# -*- coding: utf-8 -*-
"""Тесты метрик маркет-мейкинга (блок F): разложение PnL спред/инвентарь СХОДИТСЯ с
общим PnL; спред-доход в свечном бэктесте ≈0 (честно), в live по книге >0; fill-rate;
markout (adverse selection); Wilson-CI; статистика инвентаря.
Запуск: pytest -q  (или python test_mm_metrics.py)
"""
import math
import random

from app.models import Candle, GridParams
from app.engine.paper import PaperEngine, _OpenOrder
from app.engine.costmodel import CostModel
from app.config import Settings
from app.analytics import metrics as M


def synth(n=400, p0=66000.0, seed=5):
    rnd = random.Random(seed)
    out, p = [], p0
    ts = 1_700_000_000_000
    for i in range(n):
        p = max(1000.0, p + math.sin(i / 20) * 40 + rnd.uniform(-150, 150))
        o = p
        h = o + abs(rnd.uniform(0, 110))
        l = o - abs(rnd.uniform(0, 110))
        c = l + rnd.random() * (h - l)
        out.append(Candle(ts=ts + i * 900_000, o=o, h=h, l=l, c=c, v=rnd.uniform(50, 300)))
        p = c
    return out


def make_engine(mode="avellaneda"):
    return PaperEngine("BTCUSDT", 1000.0, GridParams(mode=mode, order_usd=80.0),
                       CostModel.from_settings(Settings()), funding={})


def _run_bt(mode="avellaneda"):
    cs = synth()
    W = 200
    e = make_engine(mode)
    for c in cs[:W]:
        e.ind.update(c)
    e._last_price = cs[W - 1].c
    e.run(cs[W:])
    return e


def test_pnl_decomposition_reconciles():
    """ГЛАВНОЕ: spread_pnl + inventory_pnl − fees − funding == общий PnL (equity − alloc)."""
    e = _run_bt("avellaneda")
    mm = e.mm_metrics()
    pnl = e.equity() - e.alloc
    assert abs(mm["net_pnl"] - pnl) < 1e-2, f"разложение не сходится: net={mm['net_pnl']} pnl={pnl}"
    tp = e.pos.realized + e.unrealized_money(e._last_price)
    assert abs(mm["total_price_pnl"] - tp) < 1e-2, "price-PnL != realized+unrealized"
    assert e.trades > 0


def test_decomposition_unavailable_in_candle_backtest():
    """В свечном бэктесте L2-книги нет, справедливую середину взять неоткуда — значит
    разложение на спред и инвентарь НЕ СЧИТАЕТСЯ.

    Раньше вместо mid подставлялась цена пробившего тик уровня. Для buy-лимитки такая
    «середина» всегда не выше цены заявки, поэтому спред-доход выходил структурно
    отрицательным (на контрольном прогоне −$1674 при ценовом PnL −$651) и вводил
    в заблуждение. Честное «не считается» лучше уверенно неверного числа."""
    mm = _run_bt("avellaneda").mm_metrics()
    assert mm["decomposition_available"] is False
    assert mm["spread_pnl"] is None and mm["inventory_pnl"] is None
    assert mm["avg_spread_per_fill"] is None
    # Сумма при этом обязана остаться верной — на ней держится сходимость эквити.
    assert isinstance(mm["total_price_pnl"], float)


def test_spread_positive_with_live_book():
    """Live: мейкер купил НИЖЕ mid книги -> положительный спред-доход."""
    cs = synth(300)
    e = make_engine("avellaneda")
    for c in cs:
        e.ind.update(c)
    px = cs[-1].c
    e._last_price = px
    e.active = True
    e.trade_driven = True
    e.quoter.last_step = 0.0
    e.ob_book = {"b": [[px - 1.0, 10.0]], "a": [[px + 1.0, 10.0]]}   # mid = px
    qty = e._order_qty(px)
    e.orders = [_OpenOrder("buy", px - 1.0, qty, 1, queue_ahead=0.0)]
    e._quote_anchor = (px, e.ind.sigma, 0.0)
    e.process_trade(px - 1.0, qty * 5, "Sell", cs[-1].ts)            # продажа в наш бид
    mm = e.mm_metrics()
    assert mm["spread_pnl"] > 0, f"ожидался положительный спред-доход, получено {mm['spread_pnl']}"


def test_fill_rate_bounds():
    e = _run_bt("avellaneda")
    mm = e.mm_metrics()
    assert mm["posted"] > 0
    assert 0.0 < mm["fill_rate"] <= 1.0
    assert mm["filled"] <= mm["posted"]


def test_markout_adverse_selection():
    """markout<0, когда цена ушла против мейкера после филла."""
    cs = synth(300)
    e = make_engine("avellaneda")
    for c in cs:
        e.ind.update(c)
    px = cs[-1].c
    e._last_price = px
    e.active = True
    e.mk_horizon_ms = 1000
    e.quoter.last_step = 0.0
    lvl = px - 0.3 * e.ind.atr
    e.orders = [_OpenOrder("buy", lvl, e._order_qty(px), 1, queue_ahead=0.0)]
    e._quote_anchor = (px, e.ind.sigma, 0.0)
    base = cs[-1].ts
    e.process_trade(lvl, 999.0, "Sell", base)      # филл buy на lvl
    e.active = False
    e.orders = []                                  # больше не торгуем — только дозреет markout
    e.on_tick(lvl - 50.0, base + 2000)             # цена ниже -> против покупателя
    mm = e.mm_metrics()
    assert mm["avg_markout_bps"] is not None and mm["avg_markout_bps"] < 0
    assert mm["adverse_rate"] == 1.0


def test_wilson_ci():
    assert M.wilson_ci(0, 0) == (0.0, 1.0)          # нет данных
    lo, hi = M.wilson_ci(5, 10)
    assert 0.0 < lo < 0.5 < hi < 1.0
    lo2, hi2 = M.wilson_ci(50, 100)
    assert (hi2 - lo2) < (hi - lo)                  # больше выборка -> уже интервал


def test_inventory_stats():
    s = M.inventory_stats([0.0, 1.0, -1.0, 2.0, 0.0])
    assert s["n"] == 5
    assert abs(s["mean"] - 0.4) < 1e-9
    assert abs(s["mean_abs"] - 0.8) < 1e-9
    assert s["max_abs"] == 2.0
    assert s["share_flat"] == 0.4
    z = M.inventory_stats([])
    assert z["n"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (mm metrics)")
