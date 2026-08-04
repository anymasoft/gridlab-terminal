# -*- coding: utf-8 -*-
"""Единый Quoter: Live (on_tick) и бэктест (step) строят сетку ОДНИМ кодом, поэтому
при идентичном состоянии дают ОДИНАКОВЫЕ котировки.

ВАЖНО (бриф claude_code_mm_engine.md): A-S держит ОДНУ пару bid/ask — осознанное
решение владельца (исполнилась -> переставляем следующую), а НЕ многоуровневую
сетку. Поэтому тесты проверяют ровно 1 buy + 1 sell, а не «многоуровневость».

Запуск: pytest -q  (или python test_quoter_unify.py)
"""
import math
import random

from app.models import Candle, GridParams
from app.engine.paper import PaperEngine
from app.engine.costmodel import CostModel
from app.config import Settings

MODES = ("heuristic", "avellaneda", "avellaneda_legacy")


def synth_candles(n=300, p0=66000.0, seed=7):
    rnd = random.Random(seed)
    out, p = [], p0
    ts = 1_700_000_000_000
    for i in range(n):
        drift = math.sin(i / 25) * 30
        p = max(1000.0, p + drift + rnd.uniform(-120, 120))
        o = p
        h = o + abs(rnd.uniform(0, 90))
        l = o - abs(rnd.uniform(0, 90))
        c = l + rnd.random() * (h - l)
        out.append(Candle(ts=ts + i * 900_000, o=o, h=h, l=l, c=c, v=rnd.uniform(50, 300)))
        p = c
    return out


def make_engine(mode):
    s = Settings()
    cost = CostModel.from_settings(s)
    p = GridParams(mode=mode, levels=8, max_orders=8, order_usd=80.0)
    return PaperEngine("BTCUSDT", 1000.0, p, cost, {})


def grid_of(eng):
    return sorted([(round(o.price, 2), o.side, o.level) for o in eng.orders if not o.manual])


def test_identical_quotes_live_eq_backtest():
    """Один и тот же Quoter из Live и бэктеста при ИДЕНТИЧНОМ состоянии -> одинаковые
    котировки. И ровно 1 buy + 1 sell (одна пара — решение владельца)."""
    cs = synth_candles()
    warm, last = cs[:-1], cs[-1]
    for mode in MODES:
        bt = make_engine(mode)
        for c in warm:
            bt.ind.update(c)
        lv = make_engine(mode)
        for c in warm:
            lv.ind.update(c)
        center = last.o
        bt._install_grid(center, last.ts)
        lv._install_grid(center, last.ts)
        g_bt, g_lv = grid_of(bt), grid_of(lv)
        assert g_bt == g_lv, f"[{mode}] сетки разошлись Live!=бэктест:\n bt={g_bt}\n lv={g_lv}"
        assert g_bt, f"[{mode}] сетка пустая"
        sides = sorted(s for _, s, _ in g_bt)
        assert sides == ["buy", "sell"], f"[{mode}] ожидалась ОДНА пара bid/ask, получено {g_bt}"


def test_inventory_skew():
    """Инвентарный скос: при лонге сетка смещается (reservation против позиции)."""
    cs = synth_candles()
    for mode in MODES:
        flat = make_engine(mode)
        for c in cs[:-1]:
            flat.ind.update(c)
        longp = make_engine(mode)
        for c in cs[:-1]:
            longp.ind.update(c)
        center = cs[-1].o
        flat._install_grid(center, cs[-1].ts)
        longp.pos.qty = 0.05
        longp.pos.avg_entry = center
        longp._install_grid(center, cs[-1].ts)
        assert grid_of(flat) != grid_of(longp), f"[{mode}] инвентарь не влияет на сетку (skew не работает)"


def test_modes_differ():
    """Переключатель режима реально влияет: canonical != heuristic != legacy."""
    cs = synth_candles()
    engs = {}
    for mode in MODES:
        e = make_engine(mode)
        for c in cs[:-1]:
            e.ind.update(c)
        e._install_grid(cs[-1].o, cs[-1].ts)
        engs[mode] = grid_of(e)
    assert engs["heuristic"] != engs["avellaneda"], "canonical A-S == heuristic (mode не влияет)"
    assert engs["avellaneda"] != engs["avellaneda_legacy"], "canonical == legacy (замена самоделки не видна)"


def test_canonical_distance_commensurate_with_sigma():
    """Канонический режим: дистанция котировок соразмерна σ. При вдвое большей σ
    полуспред заметно шире (а не фиксированные множители ATR прежней самоделки)."""
    def half_spread_for(seed_scale):
        rnd = random.Random(11)
        out, p = [], 66000.0
        ts = 1_700_000_000_000
        for i in range(300):
            p = max(1000.0, p + rnd.uniform(-120, 120) * seed_scale)
            o = p
            h = o + abs(rnd.uniform(0, 90)) * seed_scale
            l = o - abs(rnd.uniform(0, 90)) * seed_scale
            c = l + rnd.random() * (h - l)
            out.append(Candle(ts=ts + i * 900_000, o=o, h=h, l=l, c=c, v=200.0))
            p = c
        e = make_engine("avellaneda")
        for cc in out[:-1]:
            e.ind.update(cc)
        center = out[-1].o
        e._install_grid(center, out[-1].ts)
        buys = [o.price for o in e.orders if o.side == "buy" and not o.manual]
        sells = [o.price for o in e.orders if o.side == "sell" and not o.manual]
        return (max(sells) - min(buys)) / 2.0, e.ind.sigma

    hs_lo, sig_lo = half_spread_for(1.0)
    hs_hi, sig_hi = half_spread_for(2.5)
    assert sig_hi > sig_lo, "контроль: высокая σ должна быть больше низкой"
    assert hs_hi > hs_lo, f"полуспред не вырос с σ: lo={hs_lo:.1f}(σ={sig_lo:.4f}) hi={hs_hi:.1f}(σ={sig_hi:.4f})"


def test_live_run_single_pair():
    """Live прогон по тикам: держится ОДНА пара bid/ask, прогон не падает."""
    cs = synth_candles(n=500)
    eng = make_engine("avellaneda")
    for c in cs[:200]:
        eng.ind.update(c)
        eng._last_price = c.c
    eng.start_strategy(cs[200].c)
    n0 = sum(1 for o in eng.orders if not o.manual)
    assert n0 == 2, f"старт должен дать ровно одну пару bid/ask, получено {n0}"
    for c in cs[200:]:
        eng.ind.update(c)
        for px in (c.o, c.h, c.l, c.c):
            eng.on_tick(px, c.ts)
    live_grid = sum(1 for o in eng.orders if not o.manual)
    assert live_grid <= 2, f"в книге должно быть <=1 пары, получено {live_grid}"
    assert eng.trades >= 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (quoter unify, single-pair)")
