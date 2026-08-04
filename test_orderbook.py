# -*- coding: utf-8 -*-
"""Тесты живого L2-стакана (app/marketdata/orderbook.py), блок B.
Офлайн: применение snapshot/delta, удаление уровня size=0, лучшие bid/ask, разбор
сделок и колбэки, и РЕКОННЕКТ с восстановлением книги через фейковый WS-поток.
Запуск: pytest -q  (или python test_orderbook.py)
"""
import asyncio

from app.config import Settings
from app.marketdata import orderbook as ob
from app.marketdata.orderbook import OrderBookFeed, OrderBookL2


def _snap(topic="orderbook.50.BTCUSDT"):
    return {"topic": topic, "type": "snapshot",
            "data": {"b": [["100", "1"], ["99", "2"]],
                     "a": [["101", "1"], ["102", "2"]], "u": 1, "seq": 1}}


def test_snapshot_best_mid_spread():
    book = OrderBookL2()
    book.apply("snapshot", _snap()["data"])
    assert book.best_bid == 100.0
    assert book.best_ask == 101.0
    assert book.mid == 100.5
    assert book.spread == 1.0
    assert book.is_valid()


def test_delta_update_and_delete():
    book = OrderBookL2()
    book.apply("snapshot", _snap()["data"])
    # delta: удалить бид 100 (size 0), добавить бид 98=3, изменить аск 101=5
    book.apply("delta", {"b": [["100", "0"], ["98", "3"]], "a": [["101", "5"]], "u": 2})
    assert 100.0 not in book.bids, "уровень с size=0 должен удаляться"
    assert book.bids[98.0] == 3.0
    assert book.asks[101.0] == 5.0
    assert book.best_bid == 99.0          # 100 удалён -> лучший бид 99
    assert book.update_id == 2


def test_top_n_sorted_format():
    book = OrderBookL2()
    book.apply("snapshot", _snap()["data"])
    top = book.top(1)
    assert top["b"] == [[100.0, 1.0]]      # лучший бид сверху
    assert top["a"] == [[101.0, 1.0]]      # лучший аск сверху
    assert set(top.keys()) == {"a", "b"}   # формат как у fetch_orderbook


def test_delta_before_snapshot_treated_as_snapshot():
    """Если первый кадр — delta (пустая книга), он трактуется как snapshot (восстановление)."""
    book = OrderBookL2()
    book.apply("delta", _snap()["data"])
    assert book.is_valid()
    assert book.best_bid == 100.0


def test_feed_dispatch_trades_and_callback():
    feed = OrderBookFeed(Settings())
    got = []
    feed.on_trade(lambda t: got.append(t))
    feed._dispatch({"topic": "publicTrade.BTCUSDT", "type": "snapshot",
                    "data": [{"T": 123, "s": "BTCUSDT", "S": "Buy", "v": "0.5", "p": "100.5", "i": "t1"},
                             {"T": 124, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "100.0", "i": "t2"}]})
    assert feed.trade_count == 2
    assert len(got) == 2
    assert got[0].side == "Buy" and got[0].price == 100.5 and got[0].size == 0.5
    assert feed.recent_trades(1)[0].side == "Sell"


def test_feed_orderbook_updates_counter():
    feed = OrderBookFeed(Settings())
    feed._dispatch(_snap())
    feed._dispatch({"topic": "orderbook.50.BTCUSDT", "type": "delta",
                    "data": {"b": [["100", "0"]], "a": [], "u": 2}})
    assert feed.updates == 2
    assert feed.book.best_bid == 99.0
    st = feed.stats()
    assert st["bids"] >= 1 and st["asks"] >= 1 and st["best_ask"] == 101.0


def _make_fake_stream(scripts):
    """Фейковый WS: на попытке i проигрывает scripts[i] и (если не последняя) рвётся,
    провоцируя реконнект. Последняя попытка остаётся живой до отмены."""
    calls = {"n": 0}

    async def fake(topics, settings=None):
        i = calls["n"]
        calls["n"] += 1
        for msg in scripts[min(i, len(scripts) - 1)]:
            yield msg
        if i < len(scripts) - 1:
            raise ConnectionError("simulated drop")
        while True:
            await asyncio.sleep(0.01)

    return fake, calls


def test_feed_reconnect_rebuilds_book(monkeypatch):
    """Реконнект+бэк-офф+переподписка+ВОССТАНОВЛЕНИЕ книги: после разрыва первый кадр —
    новый snapshot, книга отстраивается заново."""
    s1 = [_snap(),
          {"topic": "orderbook.50.BTCUSDT", "type": "delta",
           "data": {"b": [["98", "3"]], "a": [], "u": 2}},
          {"topic": "publicTrade.BTCUSDT", "type": "snapshot",
           "data": [{"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "100.5", "i": "a"}]}]
    # после реконнекта — совершенно другой snapshot (докажем восстановление)
    s2 = [{"topic": "orderbook.50.BTCUSDT", "type": "snapshot",
           "data": {"b": [["200", "1"], ["199", "1"]], "a": [["201", "1"]], "u": 9, "seq": 9}}]
    fake, calls = _make_fake_stream([s1, s2])
    monkeypatch.setattr(ob.bybit, "stream_public_ws", fake)

    async def drive():
        feed = OrderBookFeed(Settings(), backoff_base=0.001, backoff_max=0.01)
        task = asyncio.create_task(feed.run(lambda: "BTCUSDT"))
        for _ in range(300):
            if calls["n"] >= 2 and feed.book.is_valid() and feed.book.best_bid == 200.0:
                break
            await asyncio.sleep(0.01)
        feed.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return feed

    feed = asyncio.run(drive())
    assert calls["n"] >= 2, "должен был переподключиться"
    assert feed.reconnects >= 1
    assert feed.book.best_bid == 200.0 and feed.book.best_ask == 201.0, "книга восстановлена из нового snapshot"
    assert feed.trade_count >= 1, "сделки из первой сессии получены"


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            sig = inspect.signature(fn)
            if "monkeypatch" in sig.parameters:
                continue   # требует pytest
            fn()
            print(f"  OK {name}")
    print("\nALL OK (orderbook, без monkeypatch-тестов — те через pytest)")
