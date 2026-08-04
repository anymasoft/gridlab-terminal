# -*- coding: utf-8 -*-
"""Тесты правки FIX_RECENTERING_AND_MODE: ордера стоят без филла, перецентровка только
после исполнения; режим по умолчанию — канонический A-S. Read-only, без сети/без :8100.
Запуск: pytest -q test_recentering.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings                       # noqa: E402
from app.models import GridParams, Candle             # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine, _OpenOrder  # noqa: E402


def _engine(mode="avellaneda"):
    s = Settings()
    p = GridParams(mode=mode, sl=0.0, max_orders=10, order_usd=80.0, grid_spacing=1.8)
    cost = CostModel.from_settings(s)
    e = PaperEngine("BTCUSDT", 1000.0, p, cost)
    # прогрев индикаторов: 40 баров с колебанием → atr>0, sigma>0
    base = 100.0
    candles = []
    for i in range(40):
        o = base + (i % 5) * 0.1
        c = o + (0.2 if i % 2 else -0.2)
        candles.append(Candle(ts=1_700_000_000_000 + i * 60_000,
                              o=o, h=o + 0.5, l=o - 0.5, c=c, v=10.0))
    e.warm(candles)
    return e


def _grid(e):
    buys = sorted(o.price for o in e.orders if not o.manual and o.side == "buy")
    sells = sorted(o.price for o in e.orders if not o.manual and o.side == "sell")
    return buys, sells


def test_default_mode_is_avellaneda():
    """Правка 2: дефолтный режим GridParams — канонический A-S."""
    assert GridParams().mode == "avellaneda"


def test_no_recenter_without_fill():
    """Тест 1: тикер уводит цену на >1 step сквозь buy БЕЗ реальной сделки (как live,
    trade_driven) → перецентровки нет, ордера стоят. До фикса _state_shifted сдвигал бы их."""
    e = _engine("avellaneda")
    e.start_strategy(e._last_price)
    buys0, sells0 = _grid(e)
    assert buys0 and sells0, "сетка A-S не выставилась"
    e.trade_driven = True                        # как live по выбранному символу (филл только из сделок)
    step = e.quoter.last_step
    assert step > 0
    center = e._last_price
    # цена идёт вниз на 2.5*step мелкими тиками — сквозь buy, но БЕЗ trade-through (process_trade)
    for i in range(1, 51):
        px = center - (2.5 * step) * (i / 50.0)
        e.on_tick(px, 1_700_000_000_000 + (40 + i) * 60_000)
    assert e.buy_filled == 0 and e.pos.qty == 0, "филла быть не должно (нет trade-through)"
    buys1, sells1 = _grid(e)
    assert buys1 == buys0 and sells1 == sells0, \
        f"ордера сдвинулись без филла: {buys0}->{buys1} / {sells0}->{sells1}"


def test_recenter_after_fill():
    """Тест 2: реальная сделка сквозь buy → филл → перецентровка; новый buy<mid<sell."""
    e = _engine("avellaneda")
    e.start_strategy(e._last_price)
    buys0, sells0 = _grid(e)
    assert buys0 and sells0
    buy0 = buys0[-1]
    posted_after_start = e.mm_posted
    # как live по выбранному символу: филлы из реальных сделок
    e.trade_driven = True
    trade_px = buy0 - 0.01                      # тейкер-продажа НИЖЕ уровня buy → trade-through
    e.process_trade(trade_px, 10.0, "Sell", 1_700_000_000_000 + 200 * 60_000)
    assert e.buy_filled > 0 and e.pos.qty > 0, "buy-лимитка не исполнилась"
    buys1, sells1 = _grid(e)
    assert buys1 and sells1, "сетка не переставлена после филла"
    buy1, sell1 = buys1[-1], sells1[0]
    assert buy1 < trade_px < sell1, f"buy {buy1} / center {trade_px} / sell {sell1}"
    assert buy1 < buy0, "новый buy должен отличаться (центр сместился вниз)"
    assert e.mm_posted > posted_after_start, "_install_grid не вызвана после филла"


def test_no_stoploss_even_if_sl_set():
    """Стоп-лосса как механизма НЕТ: даже при sl>0 в параметрах позиция НЕ закрывается
    тейкером по рынку при ходе против неё. Выход — только встречной лимиткой (грид)."""
    e = _engine("avellaneda")
    e.p.sl = 4.0                                  # даже если в сохранёнке остался sl>0
    e.start_strategy(e._last_price)
    buys0, _ = _grid(e)
    assert buys0
    buy0 = buys0[-1]
    e.trade_driven = True
    e.process_trade(buy0 - 0.01, 10.0, "Sell", 1_700_000_000_000 + 200 * 60_000)   # набрали лонг
    assert e.pos.qty > 0
    entry, atr = e.pos.avg_entry, e.ind.atr
    # цена уходит на 6*ATR против позиции (глубже порога sl=4*ATR) — стоп НЕ должен сработать
    for i in range(1, 61):
        e.on_tick(entry - 6.0 * atr * (i / 60.0), 1_700_000_000_000 + (200 + i) * 60_000)
    assert e.pos.qty > 0, "позицию закрыл стоп-лосс — а механизма быть не должно"
    assert not any(ev.action == "Stop-Loss" for ev in e.events), "появилось событие Stop-Loss"


def test_reversal_resets_avg_entry():
    """Разворот (fill больше встречной позиции): закрытая часть даёт realized, а ОСТАТОК
    открывает новую позицию по ЦЕНЕ СДЕЛКИ — avg_entry сбрасывается на price."""
    e = _engine("avellaneda")
    e._apply_fill("sell", 0.001, 64234.0, is_maker=True, ts=1, note="")
    assert e.pos.qty < 0 and abs(e.pos.avg_entry - 64234.0) < 1e-6
    e._apply_fill("buy", 0.016, 64210.0, is_maker=True, ts=2, note="")
    assert abs(e.pos.qty - 0.015) < 1e-9, f"ожидался лонг 0.015, получено {e.pos.qty}"
    assert abs(e.pos.avg_entry - 64210.0) < 1e-6, \
        f"avg_entry новой позиции должен быть 64210 (цена сделки), а не {e.pos.avg_entry}"
    assert abs(e.pos.realized - 0.024) < 1e-6, f"realized закрытого шорта = +0.024, получено {e.pos.realized}"


def test_roundtrip_close_pnl():
    """Чистое закрытие round-trip: sell@64234 → buy@64210 (ровно) → flat, realized=+0.024."""
    e = _engine("avellaneda")
    e._apply_fill("sell", 0.001, 64234.0, is_maker=True, ts=1, note="")
    e._apply_fill("buy", 0.001, 64210.0, is_maker=True, ts=2, note="")
    assert abs(e.pos.qty) < 1e-12, "позиция должна закрыться в ноль"
    assert abs(e.pos.realized - 0.024) < 1e-6, f"realized round-trip = +0.024, получено {e.pos.realized}"


def test_backtest_no_recenter_each_bar():
    """Тест 4 (паритет): бэктест-путь step() — бары внутри (buy,sell) не двигают сетку."""
    e = _engine("avellaneda")
    e.start_strategy(e._last_price)
    buys0, sells0 = _grid(e)
    assert buys0 and sells0
    buy0, sell0 = buys0[-1], sells0[0]
    posted0 = e.mm_posted
    lo = buy0 + (sell0 - buy0) * 0.3
    hi = buy0 + (sell0 - buy0) * 0.7
    midbar = (lo + hi) / 2.0
    for i in range(50):
        e.step(Candle(ts=1_700_000_000_000 + (300 + i) * 60_000,
                      o=midbar, h=hi, l=lo, c=midbar, v=10.0))
    buys1, sells1 = _grid(e)
    assert e.mm_posted == posted0, f"перецентровка каждый бар: posted {posted0}->{e.mm_posted}"
    assert buys1 == buys0 and sells1 == sells0, "ордера сдвинулись без филла (бэктест)"


def test_swept_buy_fills_when_trade_prints_below_level():
    """Правка 5: реальная сделка прошла СТРОГО НИЖЕ buy-уровня → очередь swept (обнулена),
    наш ордер исполняется на доступный объём. Отмены не видны в ленте — но уровень пуст."""
    e = _engine("avellaneda")
    e.active = True
    e.trade_driven = True
    o = _OpenOrder("buy", 100.0, 1.0, 0, queue_ahead=5.0)
    e.orders = [o]
    e.process_trade(99.5, 0.5, "Sell", 1_700_000_000_000)   # sell-принт СТРОГО ниже 100
    assert o.queue_ahead == 0.0, "очередь должна обнулиться при проходе цены за уровень"
    assert abs(e.buy_filled - 0.5) < 1e-9, f"ожидался филл 0.5, получено {e.buy_filled}"
    assert abs(e.pos.qty - 0.5) < 1e-9, f"позиция должна быть +0.5, получено {e.pos.qty}"


def test_no_swept_when_trade_at_exact_level():
    """Правка 5 (граница): сделка РОВНО на уровне — НЕ swept. Очередь тает обычно по объёму,
    филла нет (объём ушёл в очередь перед нами)."""
    e = _engine("avellaneda")
    e.active = True
    e.trade_driven = True
    o = _OpenOrder("buy", 100.0, 1.0, 0, queue_ahead=5.0)
    e.orders = [o]
    e.process_trade(100.0, 0.5, "Sell", 1_700_000_000_000)   # РОВНО на уровне
    assert abs(o.queue_ahead - 4.5) < 1e-9, f"очередь должна стаять 5.0->4.5, стало {o.queue_ahead}"
    assert e.buy_filled == 0.0, "филла быть не должно — объём ушёл в очередь перед нами"
    assert e.pos.qty == 0.0


def test_partial_fill_holds_order_full_fill_recenters():
    """Правка 6: partial fill НЕ перецентрует сетку — ордер остаётся с уменьшенным size и
    добирается следующими сделками. Только ПОЛНЫЙ fill (ордер снят) → _install_grid."""
    e = _engine("avellaneda")
    e.active = True
    e.trade_driven = True
    o = _OpenOrder("sell", 100.0, 0.016, 0, queue_ahead=0.0)
    e.orders = [o]
    posted0 = e.mm_posted
    # partial: buy-принт через sell-уровень, объём 0.001 из 0.016
    e.process_trade(100.0, 0.001, "Buy", 1_700_000_000_000)
    assert abs(o.size - 0.015) < 1e-9, f"остаток ордера должен быть 0.015, стало {o.size}"
    assert o in e.orders, "частичный ордер должен оставаться на месте"
    assert e.mm_posted == posted0, "перецентровки при partial быть не должно"
    # добор: остаток 0.015 исполняется полностью → ордер снят → перецентровка
    e.process_trade(100.0, 0.015, "Buy", 1_700_000_000_001)
    assert o not in e.orders, "при полном филле ордер должен сняться"
    assert e.mm_posted > posted0, "после полного филла — _install_grid (перецентровка)"
