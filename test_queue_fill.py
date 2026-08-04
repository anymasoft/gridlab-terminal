# -*- coding: utf-8 -*-
"""Тесты честного эмулятора исполнения (app/engine/queue_fill.py), блок C.
trade-through, тающая очередь, частичные по объёму, симметрия sell, и факт:
наивное «касание» даёт БОЛЬШЕ филлов, чем честный trade-through.
Запуск: pytest -q  (или python test_queue_fill.py)
"""
from app.marketdata.orderbook import Trade
from app.engine.queue_fill import (
    TradeThroughFiller, NaiveTouchFiller, fill_metrics, book_queue_ahead,
)


def T(ts, price, size, side):
    return Trade(ts=ts, price=price, size=size, side=side)


def test_buy_fills_on_trade_through():
    f = TradeThroughFiller()
    o = f.post("buy", 100.0, 1.0, queue_ahead=0.0)
    f.on_trade(T(1, 99.5, 1.0, "Sell"))      # тейкер продал в наш бид по цене <= уровня
    assert o.is_filled
    assert o.vwap == 100.0                    # исполнение по НАШЕМУ уровню (мейкер)


def test_no_fill_wrong_side_or_above_level():
    f = TradeThroughFiller()
    o = f.post("buy", 100.0, 1.0, queue_ahead=0.0)
    f.on_trade(T(1, 99.5, 5.0, "Buy"))       # покупка — не исполняет buy-лимитку
    f.on_trade(T(2, 100.5, 5.0, "Sell"))     # продажа, но ВЫШЕ уровня — цена не дошла
    assert o.filled == 0.0


def test_no_fill_on_drift_without_trade():
    """Дрейф котировок без сделки филла не даёт (нет встречного контрагента)."""
    f = TradeThroughFiller()
    o = f.post("buy", 100.0, 1.0, queue_ahead=0.0)
    # ни одной сделки не подано -> ничего не исполнено
    assert o.filled == 0.0 and not o.done


def test_queue_melts_then_fills():
    f = TradeThroughFiller()
    o = f.post("buy", 100.0, 1.0, queue_ahead=2.0)
    f.on_trade(T(1, 100.0, 1.0, "Sell"))     # тает очередь 2->1, нам ничего
    assert o.filled == 0.0 and o.queue_ahead == 1.0
    f.on_trade(T(2, 100.0, 1.0, "Sell"))     # тает 1->0, остаток 0 -> нам ничего
    assert o.filled == 0.0 and o.queue_ahead == 0.0
    f.on_trade(T(3, 100.0, 1.5, "Sell"))     # очередь пуста -> 1.0 нам (остаток 0.5 мимо)
    assert o.is_filled


def test_partial_fills_by_volume():
    f = TradeThroughFiller()
    o = f.post("buy", 100.0, 2.0, queue_ahead=0.0)
    f.on_trade(T(1, 99.0, 0.5, "Sell"))
    assert abs(o.filled - 0.5) < 1e-9 and not o.is_filled
    f.on_trade(T(2, 99.0, 1.0, "Sell"))
    assert abs(o.filled - 1.5) < 1e-9
    f.on_trade(T(3, 99.0, 1.0, "Sell"))      # дольёт до 2.0 (0.5 мимо)
    assert o.is_filled and o.n_fills == 3


def test_sell_side_symmetric():
    f = TradeThroughFiller()
    o = f.post("sell", 100.0, 1.0, queue_ahead=0.0)
    f.on_trade(T(1, 99.5, 1.0, "Buy"))       # покупка ниже уровня — не исполняет sell
    assert o.filled == 0.0
    f.on_trade(T(2, 100.5, 1.0, "Buy"))      # тейкер купил через наш аск
    assert o.is_filled


def _post_set(f):
    f.post("buy", 100.0, 1.0, queue_ahead=2.0)
    f.post("buy", 95.0, 1.0, queue_ahead=10.0)


def _stream():
    return [T(1, 100.0, 1.0, "Sell"), T(2, 99.0, 2.0, "Sell"), T(3, 95.0, 1.0, "Sell")]


def test_naive_fills_more_than_honest():
    """Ключевой факт блока C: наивное касание даёт БОЛЬШЕ филлов, чем честный trade-through."""
    honest = TradeThroughFiller(); _post_set(honest)
    naive = NaiveTouchFiller(); _post_set(naive)
    for tr in _stream():
        honest.on_trade(tr)
        naive.on_trade(tr)
    h = sum(1 for o in honest.orders if o.filled > 0)
    n = sum(1 for o in naive.orders if o.filled > 0)
    assert naive.posted == honest.posted == 2
    assert n > h, f"наивное касание должно давать больше филлов: naive={n} honest={h}"
    assert h == 1 and n == 2


def test_fill_metrics_markout_adverse():
    """markout < 0 = adverse selection (цена ушла против мейкера после филла)."""
    f = TradeThroughFiller()
    f.post("buy", 100.0, 1.0, queue_ahead=0.0)
    trades = [T(1000, 100.0, 1.0, "Sell"), T(3000, 98.0, 1.0, "Sell")]
    for tr in trades:
        f.on_trade(tr)
    m = fill_metrics(f, trades, horizon_ms=2000)
    assert m["fill_rate"] == 1.0
    assert m["avg_markout_bps"] is not None and m["avg_markout_bps"] < 0
    assert m["adverse_rate"] == 1.0


def test_fill_rate_below_one():
    """Честный fill-rate < 100%: часть выставленных не исполняется."""
    f = TradeThroughFiller()
    _post_set(f)
    for tr in _stream():
        f.on_trade(tr)
    m = fill_metrics(f, _stream())
    assert 0.0 < m["fill_rate"] < 1.0


def test_book_queue_ahead_estimate():
    bids = [[100.0, 1.0], [99.0, 2.0], [98.0, 3.0]]
    asks = [[101.0, 1.0], [102.0, 2.0], [103.0, 4.0]]
    assert book_queue_ahead(bids, 99.0, "buy") == 3.0    # биды на ценах >= 99: 1+2
    assert book_queue_ahead(asks, 102.0, "sell") == 3.0  # аски на ценах <= 102: 1+2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (queue_fill)")
