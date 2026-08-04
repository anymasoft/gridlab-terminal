# -*- coding: utf-8 -*-
"""Тесты live-экспорта (журнал/филлы CSV + полный JSON-снимок). Read-only, без сети/без :8100.
Запуск: pytest -q test_live_export.py"""
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import storage                                 # noqa: E402
from app.config import Settings                         # noqa: E402
from app.models import GridParams, Candle               # noqa: E402
from app.engine.costmodel import CostModel              # noqa: E402
from app.engine.paper import PaperEngine                # noqa: E402


def _engine():
    s = Settings()
    p = GridParams(mode="avellaneda", sl=0.0, order_usd=80.0, grid_spacing=1.8, max_orders=10)
    e = PaperEngine("BTCUSDT", 1000.0, p, CostModel.from_settings(s))
    cs = []
    for i in range(40):
        o = 100.0 + (i % 5) * 0.1
        c = o + (0.2 if i % 2 else -0.2)
        cs.append(Candle(ts=1_700_000_000_000 + i * 60_000, o=o, h=o + 0.5, l=o - 0.5, c=c, v=10.0))
    e.warm(cs)
    return e


def _fill_some(e):
    e.start_strategy(e._last_price)        # событие «Старт стратегии»
    e.trade_driven = True
    buys = sorted(o.price for o in e.orders if o.side == "buy" and not o.manual)
    if buys:
        e.process_trade(buys[-1] - 0.01, 10.0, "Sell", 1_700_000_000_000 + 200 * 60_000)
    sells = sorted(o.price for o in e.orders if o.side == "sell" and not o.manual)
    if sells:
        e.process_trade(sells[0] + 0.01, 10.0, "Buy", 1_700_000_000_000 + 201 * 60_000)
    return e


def test_live_events_csv_has_all_rows():
    e = _fill_some(_engine())
    rows = list(csv.reader(io.StringIO(storage.live_events_csv(e.events, "BTCUSDT"))))
    assert rows[0] == ["ts", "time_msk", "action", "symbol", "price", "size", "fee", "pnl", "comment"]
    assert len(rows) - 1 == len(e.events) and len(e.events) >= 2
    assert any("Grid" in r[2] or "Старт" in r[2] for r in rows[1:])
    assert all(r[3] == "BTCUSDT" for r in rows[1:])


def test_live_fills_csv_matches_fills():
    e = _fill_some(_engine())
    rows = list(csv.reader(io.StringIO(storage.live_fills_csv(e.fills, "BTCUSDT"))))
    assert rows[0][:6] == ["ts", "time_msk", "order_id", "symbol", "side", "price"]
    assert len(rows) - 1 == len(e.fills) and len(e.fills) >= 1
    assert all(r[8] in ("0", "1") for r in rows[1:])   # is_maker как 0/1


def test_live_bundle_json_complete():
    e = _fill_some(_engine())
    meta = {"symbol": "BTCUSDT", "capital": 1000.0, "started": True,
            "params": e.p.model_dump(), "exported_ms": 1}
    out = storage.live_bundle_json(meta, e.summary(), e.mm_metrics(), e.events, e.fills,
                                   "BTCUSDT", [1000.0, round(e.equity(), 4)], [1000.0, round(e.cash, 4)])
    d = json.loads(out)
    assert d["meta"]["symbol"] == "BTCUSDT"
    assert "mm_metrics" in d and "summary" in d
    assert len(d["events"]) == len(e.events)
    assert len(d["fills"]) == len(e.fills)
    assert d["equity_curve"][0] == 1000.0 and len(d["balance_curve"]) == 2
    # контроль: net_pnl из mm-метрик = equity - capital (net_pnl округлён до 4 знаков)
    assert abs(d["mm_metrics"]["net_pnl"] - (e.equity() - 1000.0)) < 1e-3
