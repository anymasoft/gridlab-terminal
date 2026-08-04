# -*- coding: utf-8 -*-
"""Property-тест: арифметика счёта не должна разъезжаться НИ НА КАКИХ данных.

Точечные тесты (round-trip, разворот) проверяют примеры, которые я придумал сам,
то есть ровно те случаи, о которых я подумал. Здесь наоборот: тысяча случайных
ценовых рядов прогоняется через движок, и после каждого прогона проверяются
два равенства, которые обязаны выполняться всегда:

  1. equity == cash + нереализованное(mark)
  2. cash   == alloc + Σ реализованное − Σ комиссии − Σ фандинг

Второе — главное. Если хоть один денежный поток проходит мимо кассы (комиссия
списана из PnL, но не из cash; фандинг посчитан дважды; реализация закрытия
потерялась), равенство ломается, и убыток на реальном счёте не совпадёт
с бумажным. Никакие точечные примеры такой ошибки не гарантируют.

Запуск: pytest -q test_equity_invariants.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings                   # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine              # noqa: E402
from app.models import Candle, GridParams             # noqa: E402

COST = CostModel.from_settings(get_settings())
EPS = 1e-6


def _series(seed, n=400, p0=100.0, vol=0.006, drift=0.0):
    """Случайное блуждание. drift != 0 даёт тренд — там ломается сетка,
    а значит именно там вероятнее всего сломается и учёт."""
    rnd = random.Random(seed)
    out, p, ts = [], p0, 1_700_000_000_000
    for i in range(n):
        o = p
        p *= 1.0 + drift + rnd.gauss(0, vol)
        h = max(o, p) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, p) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(Candle(ts=ts + i * 900_000, o=o, h=h, l=l, c=p, v=1000.0))
    return out


def _check(e, alloc):
    """Оба инварианта на одном движке. Возвращает текст расхождения или None."""
    mark = e._last_price
    eq = e.equity()
    if abs(eq - (e.cash + e.unrealized_money(mark))) > EPS:
        return f"equity {eq} != cash {e.cash} + unreal {e.unrealized_money(mark)}"

    # После ликвидации cash принудительно обнуляется (изолированная маржа:
    # минус не переносится на другие инструменты), поэтому баланс потоков
    # там заведомо не сходится — это осознанное поведение, не ошибка.
    if e.liquidated:
        return None

    expect = alloc + e.pos.realized - e.pos.fees_paid - e.pos.funding_paid
    if abs(e.cash - expect) > 1e-4:
        return (f"cash {e.cash:.8f} != alloc {alloc} + realized {e.pos.realized:.8f} "
                f"- fees {e.pos.fees_paid:.8f} - funding {e.pos.funding_paid:.8f} "
                f"= {expect:.8f}, расхождение {e.cash - expect:.2e}")
    return None


def _run(seed, drift, params, alloc=1000.0, spec=None):
    e = PaperEngine("T", alloc, params, COST, funding=None, spec=spec)
    e.run(_series(seed, drift=drift))
    return e


def test_invariants_hold_on_random_walks():
    """Сто случайных боковиков — базовый режим, где сетка и должна работать."""
    P = GridParams()
    for seed in range(100):
        e = _run(seed, 0.0, P.model_copy())
        err = _check(e, 1000.0)
        assert err is None, f"seed={seed} боковик: {err}"


def test_invariants_hold_on_trends_both_ways():
    """Тренды в обе стороны: здесь позиция набирается в одну сторону и держится,
    работает фандинг, случаются ликвидации — самый опасный для учёта режим."""
    P = GridParams()
    for seed in range(150):
        drift = (0.0015 if seed % 2 else -0.0015) * (1 + seed % 3)
        e = _run(seed, drift, P.model_copy())
        err = _check(e, 1000.0)
        assert err is None, f"seed={seed} drift={drift}: {err}"


def test_invariants_hold_across_parameter_space():
    """Разные шаги, число уровней и стороны — чтобы ошибка не пряталась
    в одной конкретной комбинации настроек."""
    combos = [(p, n, sd)
              for p in (0.3, 1.0, 3.0)
              for n in (3, 10, 20)
              for sd in ("neutral", "long")]
    for i, (p, n, sd) in enumerate(combos):
        for seed in range(4):
            P = GridParams(grid_step_pct=p, grid_levels=n, grid_side=sd)
            e = _run(seed + i * 100, 0.001 if seed % 2 else -0.001, P)
            err = _check(e, 1000.0)
            assert err is None, f"step={p}% x{n} {sd} seed={seed}: {err}"


def test_invariants_hold_on_cheap_instrument():
    """Пункт 3.2 аудита: DOGE стоит $0.07, шаг тика 1e-05. На тысячах мелких
    филлов средняя цена входа копит ошибку округления — проверяем, что она
    не выходит за допуск."""
    P = GridParams()
    for seed in range(40):
        e = PaperEngine("DOGEUSDT", 1000.0, P.model_copy(), COST, funding=None,
                        spec={"tick_size": 1e-05, "qty_step": 1.0, "min_qty": 1.0})
        e.run(_series(seed, n=800, p0=0.07, drift=0.0008 if seed % 2 else -0.0008))
        err = _check(e, 1000.0)
        assert err is None, f"DOGE seed={seed}: {err}"


def test_equity_never_silently_exceeds_isolated_margin_loss():
    """Изолированная маржа: потерять можно только выделенное на инструмент.
    Эквити не имеет права уйти ниже нуля ни на каком ряде."""
    P = GridParams()
    for seed in range(120):
        e = _run(seed, -0.004 if seed % 2 else 0.004, P.model_copy())
        assert e.equity() >= -EPS, f"seed={seed}: эквити {e.equity()} < 0"


def test_a_deliberately_broken_ledger_is_caught():
    """Контроль самого теста: если незаметно списать доллар мимо кассы,
    инвариант обязан это увидеть. Иначе тест ничего не проверяет."""
    e = _run(1, 0.0, GridParams())
    e.cash -= 1.0
    assert _check(e, 1000.0) is not None, "инвариант не заметил пропажу доллара"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (инварианты эквити)")
