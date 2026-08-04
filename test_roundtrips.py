# -*- coding: utf-8 -*-
"""Журнал закрытых пар: «взяли по 1000, отдали по 1200, заработали 200».

Список исполнений на этот вопрос не отвечает: в нём покупка и продажа лежат
порознь, и человеку приходится складывать их в уме. Здесь каждая закрытая пара —
одна запись с обеими ногами и деньгами.

Запуск: pytest -q test_roundtrips.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings                   # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine              # noqa: E402
from app.models import GridParams                     # noqa: E402

COST = CostModel.from_settings(get_settings())


def _engine(alloc=1000.0):
    e = PaperEngine("T", alloc, GridParams(), COST, funding=None)
    e._last_price = 1000.0
    return e


def test_closed_pair_records_both_legs_and_the_money():
    """Купили по 1000, продали по 1200 — одна запись, а не две."""
    e = _engine()
    e._apply_fill("buy", 1.0, 1000.0, True, 1, "")
    assert not e.roundtrips, "пока не продали — закрытой пары нет"

    e._apply_fill("sell", 1.0, 1200.0, True, 2, "")
    assert len(e.roundtrips) == 1
    r = e.roundtrips[0]
    assert r["dir"] == "long"
    assert r["entry"] == 1000.0
    assert r["exit"] == 1200.0
    assert r["qty"] == 1.0
    assert abs(r["gross"] - 200.0) < 1e-9, "ценовая разница обязана быть ровно 200"
    assert r["fee"] > 0, "комиссии обеих ног должны быть учтены"
    assert abs(r["net"] - (200.0 - r["fee"])) < 1e-9


def test_net_matches_the_number_used_in_metrics():
    """Итог в журнале — то же число, что идёт в win rate и profit factor."""
    e = _engine()
    e._apply_fill("buy", 2.0, 500.0, True, 1, "")
    e._apply_fill("sell", 2.0, 510.0, True, 2, "")
    assert abs(e.roundtrips[-1]["net"] - e.realized_pnls[-1]) < 1e-12


def test_short_pair_is_recorded_the_other_way_round():
    """Продали по 1200, откупили по 1000 — тоже +200, направление «шорт»."""
    e = _engine()
    e._apply_fill("sell", 1.0, 1200.0, True, 1, "")
    e._apply_fill("buy", 1.0, 1000.0, True, 2, "")
    r = e.roundtrips[-1]
    assert r["dir"] == "short"
    assert r["entry"] == 1200.0 and r["exit"] == 1000.0
    assert abs(r["gross"] - 200.0) < 1e-9


def test_losing_pair_is_negative():
    e = _engine()
    e._apply_fill("buy", 1.0, 1000.0, True, 1, "")
    e._apply_fill("sell", 1.0, 900.0, True, 2, "")
    r = e.roundtrips[-1]
    assert r["gross"] < 0 and r["net"] < r["gross"], "комиссия обязана усугубить убыток"


def test_partial_close_records_only_what_was_closed():
    """Купили 3, продали 1 — в журнал идёт закрытый объём, а не вся позиция."""
    e = _engine()
    e._apply_fill("buy", 3.0, 1000.0, True, 1, "")
    e._apply_fill("sell", 1.0, 1100.0, True, 2, "")
    r = e.roundtrips[-1]
    assert r["qty"] == 1.0
    assert abs(r["gross"] - 100.0) < 1e-9
    assert abs(e.pos.qty - 2.0) < 1e-12, "остаток позиции должен сохраниться"


def test_journal_sum_equals_realized_after_fees():
    """Сумма итогов журнала совпадает с суммой чистых результатов сделок.
    Если разойдётся — журнал показывает не те деньги, что реально на счёте."""
    e = _engine()
    for i in range(20):
        e._apply_fill("buy", 0.5, 1000.0 + i, True, i * 2, "")
        e._apply_fill("sell", 0.5, 1010.0 + i, True, i * 2 + 1, "")
    assert abs(sum(r["net"] for r in e.roundtrips) - sum(e.realized_pnls)) < 1e-9


def test_journal_survives_restart():
    e = _engine()
    e._apply_fill("buy", 1.0, 1000.0, True, 1, "")
    e._apply_fill("sell", 1.0, 1200.0, True, 2, "")
    fresh = _engine()
    fresh.load_state(e.to_state())
    assert len(fresh.roundtrips) == 1
    assert fresh.roundtrips[0]["net"] == e.roundtrips[0]["net"]


def test_journal_does_not_grow_without_limit():
    """Журнал — хвост для чтения, а не хранилище: он не должен раздувать сессию."""
    e = _engine(alloc=1_000_000.0)
    for i in range(1200):
        e._apply_fill("buy", 0.001, 1000.0, True, i * 2, "")
        e._apply_fill("sell", 0.001, 1001.0, True, i * 2 + 1, "")
    assert len(e.roundtrips) <= 500
    assert len(e.to_state()["roundtrips"]) <= 200
    # метрики при этом считаются по ПОЛНОЙ истории, а не по хвосту журнала
    assert len(e.realized_pnls) == 1200


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (журнал закрытых пар)")
