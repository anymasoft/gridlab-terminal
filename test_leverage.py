# -*- coding: utf-8 -*-
"""Плечо — как гарантийное обеспечение на срочном рынке.

Смысл ровно тот же, что на ФОРТС: под позицию замораживается не вся её
стоимость, а доля 1/плечо. Само плечо заявку НЕ увеличивает — размером
управляет grid_notional_mult, а плечо задаёт его потолок и то, где встанет
цена ликвидации.

Предел плеча у Bybit свой на каждый инструмент (BTC 100×, BNB 50×, DOGE 75×),
и разрешать на бумаге больше, чем даёт биржа, нельзя: это занижает риск.

Запуск: pytest -q test_leverage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings                   # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine              # noqa: E402
from app.models import GridParams                     # noqa: E402

COST = CostModel.from_settings(get_settings())


def _engine(lev=0.0, alloc=1000.0, max_lev=0.0, price=100.0):
    p = GridParams(leverage=lev)
    e = PaperEngine("T", alloc, p, COST, funding=None,
                    spec={"tick_size": 0.0, "qty_step": 0.0, "min_qty": 0.0,
                          "max_leverage": max_lev})
    e._last_price = price
    return e


def test_zero_means_take_it_from_config():
    """0 в параметре — это «как в конфиге», а не «плеча нет»."""
    assert _engine(lev=0.0).leverage() == COST.leverage


def test_explicit_leverage_wins_over_config():
    assert _engine(lev=10.0).leverage() == 10.0


def test_exchange_limit_caps_the_request():
    """Просили 100×, биржа по инструменту даёт 50× — берём 50×."""
    assert _engine(lev=100.0, max_lev=50.0).leverage() == 50.0
    assert _engine(lev=20.0, max_lev=50.0).leverage() == 20.0


def test_leverage_below_one_is_impossible():
    assert _engine(lev=0.2).leverage() == 1.0


def test_margin_is_the_position_divided_by_leverage():
    """Главное свойство: обеспечение = нотионал / плечо."""
    for lev in (1.0, 3.0, 10.0):
        e = _engine(lev=lev)
        e.active = True
        e._add_grid_order("buy", 100.0, 5.0, 1)         # нотионал $500
        assert abs(e._margin_used(100.0) - 500.0 / lev) < 1e-9, \
            f"при плече {lev} обеспечение должно быть {500.0/lev}"


def test_leverage_does_not_change_order_size():
    """На бирже плечо не увеличивает заявку — размером управляет нотионал.
    Если бы плечо входило в размер, прежние результаты прогонов стали бы
    несопоставимы, а риск вырос бы молча."""
    a = _engine(lev=1.0)._grid_size_of(100.0)
    b = _engine(lev=25.0)._grid_size_of(100.0)
    assert a == b, "размер заявки не должен зависеть от плеча"

    big = PaperEngine("T", 1000.0, GridParams(grid_notional_mult=3.0), COST,
                      funding=None)
    assert abs(big._grid_size_of(100.0) - 3 * a) < 1e-12, \
        "а вот нотионал ×3 обязан утроить заявку"


def _cap(lev, mult, alloc=100.0, price=100.0):
    p = GridParams(leverage=lev, grid_notional_mult=mult)
    e = PaperEngine("T", alloc, p, COST, funding=None)
    e._last_price = price
    return e._max_contracts(price)


def test_leverage_is_a_ceiling_not_a_size():
    """Потолок позиции — строгий минимум из двух ограничений: заказанного
    нотионала и плеча. Пока нотионал ×1, плечо ни на что не влияет —
    и это правильно: само по себе оно позицию не раздувает."""
    assert _cap(lev=2.0, mult=1.0) == _cap(lev=20.0, mult=1.0) == 1.0

    # подняли нотионал до ×10 — теперь ограничивает уже плечо
    assert _cap(lev=2.0, mult=10.0) == 2.0, "при плече 2× больше 2×alloc нельзя"
    assert _cap(lev=20.0, mult=10.0) == 10.0, "здесь ограничивает нотионал ×10"


def test_liquidation_price_is_where_the_engine_actually_liquidates():
    """Показанная цена ликвидации обязана совпасть с фактическим срабатыванием.
    Если она врёт, человек считает, что у него есть запас, которого нет."""
    e = _engine(lev=10.0, alloc=100.0)
    e.active = True
    e._apply_fill("buy", 5.0, 100.0, True, 1, "")      # нотионал $500 на $100
    liq = e.liquidation_price()
    assert 0 < liq < 100.0, f"для лонга ликвидация ниже входа, получено {liq}"

    # чуть выше расчётной цены позиция обязана выжить
    e.mark_price = liq * 1.02
    assert not e._check_liquidation(2, e.mark_price), "ликвидировали раньше времени"

    # на расчётной — закрыться
    e.mark_price = liq * 0.995
    assert e._check_liquidation(3, e.mark_price), "ликвидация не сработала там, где обещано"


def test_short_liquidation_is_above_the_entry():
    e = _engine(lev=10.0, alloc=100.0)
    e.active = True
    e._apply_fill("sell", 5.0, 100.0, True, 1, "")
    liq = e.liquidation_price()
    assert liq > 100.0, f"для шорта ликвидация выше входа, получено {liq}"


def test_more_leverage_moves_liquidation_closer():
    """Ровно тот риск, о котором предупреждает подсказка в интерфейсе."""
    def liq_at(lev, alloc):
        e = _engine(lev=lev, alloc=alloc)
        e.active = True
        e._apply_fill("buy", alloc * lev / 100.0, 100.0, True, 1, "")
        return e.liquidation_price()
    near = liq_at(20.0, 100.0)
    far = liq_at(2.0, 100.0)
    assert near > far, "при большем плече ликвидация обязана быть ближе к цене входа"


def test_no_position_means_no_liquidation_price():
    assert _engine(lev=5.0).liquidation_price() == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (плечо)")
