# -*- coding: utf-8 -*-
"""Тесты фонового архива L2-стакана и ленты сделок (app/marketdata/archive.py), блок E.
Проверяет: лента пишется и читается с сохранением СТОРОНЫ и точного времени (мс);
книга восстанавливается из snapshot+дельт на любой момент T; ротация по дням;
order-flow дисбаланс считается; дописанные gzip-члены (crash-safe) читаются все.
Запуск: pytest -q  (или python test_archive.py)
"""
import asyncio
import os
import tempfile
from pathlib import Path

from app.marketdata.orderbook import Trade
from app.marketdata import archive as A


def _msg(ty, b, a, ts, u):
    return {"topic": "orderbook.50.BTCUSDT", "type": ty, "ts": ts,
            "data": {"b": b, "a": a, "u": u, "seq": u}}


def test_trade_roundtrip_keeps_side_and_ms():
    d = tempfile.mkdtemp()
    w = A.ArchiveWriter(d)
    ts0 = 1_700_000_000_123                       # время с миллисекундами
    w.record_trade("BTCUSDT", Trade(ts=ts0, price=64000.5, size=0.01, side="Buy"))
    w.record_trade("BTCUSDT", Trade(ts=ts0 + 50, price=64000.0, size=0.2, side="Sell"))
    asyncio.run(w.flush())
    path = os.path.join(d, "trades", "BTCUSDT", f"{A._day(ts0)}.ndjson.gz")
    recs = list(A.read_ndjson_gz(path))
    assert len(recs) == 2
    assert recs[0]["t"] == ts0 and recs[0]["p"] == 64000.5 and recs[0]["v"] == 0.01
    assert recs[0]["s"] == "B" and recs[1]["s"] == "S"     # сторона-агрессор сохранена
    assert recs[1]["t"] == ts0 + 50                         # точное время (мс) сохранено


def test_book_roundtrip_and_reconstruct_at_T():
    d = tempfile.mkdtemp()
    w = A.ArchiveWriter(d)
    ts0 = 1_700_000_000_000
    w.record_book("BTCUSDT", _msg("snapshot", [["100", "1"], ["99", "2"]], [["101", "1"]], ts0, 1))
    w.record_book("BTCUSDT", _msg("delta", [["100", "0"], ["98", "3"]], [], ts0 + 100, 2))
    asyncio.run(w.flush())
    path = os.path.join(d, "orderbook", "BTCUSDT", f"{A._day(ts0)}.ndjson.gz")
    recs = list(A.read_ndjson_gz(path))
    assert len(recs) == 2 and recs[0]["ty"] == "s" and recs[1]["ty"] == "d"
    book = A.reconstruct_book(recs)
    assert book.best_bid == 99.0 and book.best_ask == 101.0      # 100 удалён дельтой
    book0 = A.reconstruct_book(recs, until_ms=ts0 + 50)          # ДО дельты
    assert book0.best_bid == 100.0                               # на момент T книга иная


def test_daily_rotation():
    d = tempfile.mkdtemp()
    w = A.ArchiveWriter(d)
    day1 = 1_700_000_000_000
    day2 = day1 + 86_400_000                       # +1 сутки
    w.record_trade("X", Trade(ts=day1, price=1.0, size=1.0, side="Buy"))
    w.record_trade("X", Trade(ts=day2, price=1.0, size=1.0, side="Sell"))
    asyncio.run(w.flush())
    files = sorted(p.name for p in (Path(d) / "trades" / "X").glob("*.ndjson.gz"))
    assert len(files) == 2, f"ожидалось 2 файла (по дню), получено {files}"


def test_order_flow_imbalance():
    recs = [{"t": 10, "v": 2.0, "s": "B"}, {"t": 20, "v": 1.0, "s": "S"},
            {"t": 30, "v": 1.0, "s": "B"}, {"t": 99, "v": 5.0, "s": "S"}]
    f = A.order_flow(recs, 0, 40)
    assert f["buy_vol"] == 3.0 and f["sell_vol"] == 1.0 and f["count"] == 3
    assert abs(f["imbalance"] - 0.5) < 1e-9        # (3-1)/4


def test_append_members_crash_safe():
    """Каждый сброс — самостоятельный gzip-член; читаются все (устойчиво к обрыву)."""
    d = tempfile.mkdtemp()
    w = A.ArchiveWriter(d)
    ts0 = 1_700_000_000_000
    w.record_trade("X", Trade(ts=ts0, price=1.0, size=1.0, side="Buy"))
    asyncio.run(w.flush())
    w.record_trade("X", Trade(ts=ts0 + 1, price=2.0, size=1.0, side="Sell"))
    asyncio.run(w.flush())                          # второй gzip-член в тот же файл
    path = os.path.join(d, "trades", "X", f"{A._day(ts0)}.ndjson.gz")
    recs = list(A.read_ndjson_gz(path))
    assert len(recs) == 2 and recs[0]["p"] == 1.0 and recs[1]["p"] == 2.0
    assert w.flushes == 2 and w.records == 2


def test_empty_flush_noop():
    w = A.ArchiveWriter(tempfile.mkdtemp())
    asyncio.run(w.flush())                          # пустой буфер -> без ошибок, без файлов
    assert w.flushes == 0 and w.disk_bytes() == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (archive)")
