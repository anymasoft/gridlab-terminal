# -*- coding: utf-8 -*-
"""Тесты риск-контроля (блок G): kill-switch по просадке закрывает позицию и
останавливает котирование; предотвращает ликвидацию; жёсткий лимит позиции не
превышается; A-S reservation смещает котировки против инвентаря (защита).
Запуск: pytest -q  (или python test_risk.py)
"""
import math
import random

from app.models import Candle, GridParams
from app.engine.paper import PaperEngine, _OpenOrder
from app.engine.costmodel import CostModel
from app.config import Settings


def synth(n=300, p0=66000.0, seed=5):
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


def eng_with(**kw):
    p = GridParams(mode="avellaneda", order_usd=80.0, **kw)
    return PaperEngine("BTCUSDT", 1000.0, p, CostModel.from_settings(Settings()), funding={})


def warm(e, cs):
    for c in cs:
        e.ind.update(c)
    e._last_price = cs[-1].c


def test_killswitch_triggers_and_closes():
    """При просадке >= порога: позиция закрыта, котирование остановлено."""
    cs = synth()
    e = eng_with(max_drawdown_pct=10.0)
    warm(e, cs)
    e.active = True
    e.peak_equity = 1000.0
    px = cs[-1].c
    e.pos.qty = (1000.0 / px) * 8.0      # навязанный лонг (накопленный инвентарь)
    e.pos.avg_entry = px
    p = px
    for i in range(20):
        p *= 0.99                         # цена падает -> растёт просадка
        e.on_tick(p, cs[-1].ts + i * 1000)
        if e.halted:
            break
    assert e.halted, "kill-switch не сработал на пороге просадки"
    assert e.pos.qty == 0.0, "позиция должна быть закрыта"
    assert not any(not o.manual for o in e.orders), "котирование должно быть остановлено"
    assert e.mm_metrics()["halted"] is True


def test_killswitch_prevents_liquidation():
    """Без kill-switch — ликвидация; с kill-switch — остановка раньше, ликвидации нет."""
    cs = synth()

    def run(thr):
        e = eng_with(max_drawdown_pct=thr, sl=0.0)
        warm(e, cs)
        e.active = True
        e.peak_equity = 1000.0
        px = cs[-1].c
        e.pos.qty = (1000.0 / px) * 30.0   # сильное плечо -> быстро к ликвидации
        e.pos.avg_entry = px
        p = px
        for i in range(15):
            p *= 0.99
            e.on_tick(p, cs[-1].ts + i * 1000)
        return e

    off = run(0.0)
    on = run(15.0)
    assert off.liquidated and not off.halted, "без kill-switch ожидалась ликвидация"
    assert on.halted and not on.liquidated, "с kill-switch ликвидации быть не должно"


def test_position_limit_hard():
    """Жёсткий лимит инвентаря (max_orders единиц) не превышается даже при затяжном наборе."""
    cs = synth()
    e = eng_with()                          # max_orders=10
    warm(e, cs)
    e.active = True
    px = cs[-1].c
    p = px
    for i in range(80):
        p *= 0.997                          # дрейф вниз -> buy-заявки набирают лонг
        e.on_tick(p, cs[-1].ts + i * 1000)
    units = abs(e.pos.qty) * e._last_price / e.p.order_usd
    assert units > 2.0, f"контроль: инвентарь должен набраться, units={units:.2f}"
    assert units <= e.p.max_orders + 1.0, f"инвентарь {units:.2f} превысил жёсткий лимит {e.p.max_orders}"


def test_allowed_qty_blocks_overfill():
    """Прямая проверка клипа: добор сверх лимита запрещён, сокращение — разрешено."""
    e = eng_with()
    e._last_price = 100.0
    cap = e._max_contracts(100.0)           # = max_orders * order_usd/price
    e.pos.qty = cap                          # уже на лимите
    assert e._allowed_qty("buy", 5.0, 100.0) == 0.0      # добор лонга запрещён
    assert e._allowed_qty("sell", 5.0, 100.0) == 5.0     # сокращение разрешено
    e.pos.qty = cap * 0.5
    assert abs(e._allowed_qty("buy", cap, 100.0) - cap * 0.5) < 1e-9  # добор только до лимита


def test_as_reservation_protects_inventory():
    """A-S reservation как защита: при лонге buy-уровень смещается НИЖЕ (осторожнее добирать)."""
    cs = synth()
    flat = eng_with()
    longp = eng_with()
    for c in cs[:-1]:
        flat.ind.update(c)
        longp.ind.update(c)
    center = cs[-1].o
    flat._install_grid(center, cs[-1].ts)
    longp.pos.qty = 0.008                 # ниже порога блокировки (~6.6 ед < max_orders 10) → buy ещё ставится
    longp.pos.avg_entry = center
    longp._install_grid(center, cs[-1].ts)
    fb = [o.price for o in flat.orders if o.side == "buy" and not o.manual]
    lb = [o.price for o in longp.orders if o.side == "buy" and not o.manual]
    assert fb and lb and max(lb) < max(fb), "при лонге buy должен сместиться ниже (reservation против позиции)"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (risk control)")
