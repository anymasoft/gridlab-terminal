# -*- coding: utf-8 -*-
"""Тесты классического грида и правок риск-контроля.

Покрыто ровно то, где ошибка тихая и дорогая — три дефекта, найденные разбором
прогона на −78%:

1. перецентровка после каждого филла превращала грид в погоню за ценой;
2. плечо из .env не проверялось нигде — фактическое доходило до 8× при LEVERAGE=3;
3. ликвидация не останавливала торговлю: счёт ликвидировался 410 раз подряд
   и уходил в минус глубже собственной аллокации.

Запуск: pytest -q test_grid_ladder.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings                       # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine              # noqa: E402
from app.models import Candle, GridParams             # noqa: E402
from app.strategy.ladder import GridLadder            # noqa: E402


def _engine(alloc=1000.0, **kw):
    # Тесты механики парности ведём на односторонней лестнице: так в стартовом наборе
    # только buy, и видно, что sell появился именно как парный TP, а не из установки.
    kw.setdefault("grid_side", "long")
    p = GridParams(mode="grid", **kw)
    e = PaperEngine("BTCUSDT", alloc, p, CostModel.from_settings(Settings()), funding={})
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


# ─────────────────────────── лестница как таковая ───────────────────────────
def test_ladder_pairs_buy_with_sell_one_step_up():
    """Ядро грида: купили на уровне — продаём РОВНО на шаг выше. Отсюда берётся
    прибыль round-trip, и она равна step × qty независимо от направления рынка."""
    lad = GridLadder(n_levels=5, side="long")
    lad.install(center=100.0, step=2.0, size_of=lambda px: 1.0)
    q = lad.pair("buy", 96.0, 1.0)
    assert q is not None and q.side == "sell"
    assert abs(q.price - 98.0) < 1e-9, f"TP должен быть на 96+2=98, получено {q.price}"
    back = lad.pair("sell", 98.0, 1.0)
    assert back is not None and back.side == "buy"
    assert abs(back.price - 96.0) < 1e-9, "продажа должна вернуть покупку на уровень ниже"


def test_ladder_levels_are_below_center_for_long():
    """Режим long: лестница покупок строго под ценой, продаж заранее нет —
    они появляются только как парный TP к фактически купленному лоту."""
    lad = GridLadder(n_levels=4, side="long")
    qs = lad.install(center=100.0, step=1.0, size_of=lambda px: 1.0)
    assert [q.side for q in qs] == ["buy"] * 4
    assert sorted(q.price for q in qs) == [96.0, 97.0, 98.0, 99.0]


def test_ladder_neutral_quotes_both_sides():
    lad = GridLadder(n_levels=3, side="neutral")
    qs = lad.install(center=100.0, step=1.0, size_of=lambda px: 1.0)
    assert sum(1 for q in qs if q.side == "buy") == 3
    assert sum(1 for q in qs if q.side == "sell") == 3


def test_default_side_is_neutral():
    """Дефолт — симметричная сетка. Односторонняя простаивает, когда цена уходит выше
    всей лестницы: покупать нечего, продавать нечего, сделок ноль."""
    assert GridParams().grid_side == "neutral"


def test_neutral_grid_works_when_price_runs_up():
    """Цена уходит ВВЕРХ выше стартового центра. Односторонняя лестница молчит,
    симметричная торгует — это и есть причина смены дефолта."""
    def run(side):
        e = _engine(grid_side=side)
        e.start_strategy(e._last_price)
        px = e._last_price
        for i in range(300):
            px *= 1.001                    # затяжной подъём
            e.on_tick(px, 1_700_000_000_000 + (100 + i) * 60_000)
        return e.buy_filled + e.sell_filled

    assert run("long") == 0.0, "контроль: односторонняя сетка на подъёме не торгует"
    assert run("neutral") > 0.0, "симметричная сетка обязана торговать на подъёме"


def test_ladder_reanchor_off_by_default():
    """grid_reanchor=0 → лестница не переставляется никогда, как бы далеко
    ни ушла цена. Это дефолт: ре-анкор фиксирует убыток набранного инвентаря."""
    lad = GridLadder(n_levels=10, side="long")
    lad.install(center=100.0, step=1.0, size_of=lambda px: 1.0)
    assert lad.out_of_range(1.0, span_mult=0.0) is False
    assert lad.out_of_range(1000.0, span_mult=0.0) is False
    assert lad.out_of_range(1000.0, span_mult=1.0) is True


# ─────────────────────────── движок: без перецентровки ───────────────────────
def test_grid_does_not_recenter_after_fill():
    """ГЛАВНАЯ правка. В MM-режиме филл вызывал _install_grid и сдвигал ВСЮ сетку
    на текущую цену. В гриде уровни обязаны остаться на месте — двигается только
    исполненный: вместо него встаёт парный TP."""
    e = _engine()
    e.start_strategy(e._last_price)
    buys0, sells0 = _grid(e)
    assert buys0 and not sells0, "в режиме long стартует только лестница покупок"
    step = e.ladder.step
    top_buy = buys0[-1]

    e.trade_driven = True
    e.process_trade(top_buy - 1e-6, 10.0, "Sell", 1_700_000_000_000 + 10 ** 6)
    assert e.pos.qty > 0, "buy-лимитка не исполнилась"

    buys1, sells1 = _grid(e)
    assert top_buy not in buys1, "исполненный уровень должен сняться"
    assert buys1 == [b for b in buys0 if b != top_buy], \
        f"остальные уровни сдвинулись: {buys0} -> {buys1}"
    assert len(sells1) == 1 and abs(sells1[0] - (top_buy + step)) < 1e-9, \
        f"парный TP должен встать на {top_buy + step}, получено {sells1}"


def test_grid_roundtrip_earns_one_step():
    """Полный round-trip: покупка на уровне и продажа парным TP дают ровно шаг сетки
    минус две maker-комиссии. Проверяем и то, что комиссия — малая доля прибыли."""
    e = _engine()
    e.start_strategy(e._last_price)
    step = e.ladder.step
    top_buy = _grid(e)[0][-1]
    e.trade_driven = True
    e.process_trade(top_buy - 1e-6, 10.0, "Sell", 1_700_000_000_001)
    qty = e.pos.qty
    tp = _grid(e)[1][0]
    e.process_trade(tp + 1e-6, 10.0, "Buy", 1_700_000_000_002)

    assert abs(e.pos.qty) < 1e-12, "позиция должна закрыться"
    expected = qty * step
    assert abs(e.pos.realized - expected) < 1e-9, \
        f"round-trip должен дать step×qty={expected}, получено {e.pos.realized}"
    assert e.pos.fees_paid < expected * 0.1, \
        (f"комиссии {e.pos.fees_paid} съели больше 10% прибыли сделки {expected} — "
         f"шаг сетки слишком мал относительно комиссии")


def test_grid_sell_fill_restores_buy_level():
    """После срабатывания TP покупка возвращается на свой уровень — лестница
    самовосстанавливается и готова отработать следующее колебание."""
    e = _engine()
    e.start_strategy(e._last_price)
    top_buy = _grid(e)[0][-1]
    e.trade_driven = True
    e.process_trade(top_buy - 1e-6, 10.0, "Sell", 1_700_000_000_001)
    tp = _grid(e)[1][0]
    e.process_trade(tp + 1e-6, 10.0, "Buy", 1_700_000_000_002)
    buys = _grid(e)[0]
    assert any(abs(b - top_buy) < 1e-9 for b in buys), \
        f"уровень {top_buy} должен вернуться в лестницу, есть {buys}"


# ─────────────────────────── шаг не зависит от таймфрейма ───────────────────────────
def _warm(e, atr_scale):
    """Прогреть индикаторы свечами заданного размаха — имитация разных таймфреймов:
    на 15m свеча в разы шире, чем на 1m, а значит и ATR."""
    base, cs = 100.0, []
    for i in range(60):
        o = base + (i % 5) * 0.1 * atr_scale
        c = o + (0.2 if i % 2 else -0.2) * atr_scale
        cs.append(Candle(ts=1_700_000_000_000 + i * 60_000,
                         o=o, h=o + 0.5 * atr_scale, l=o - 0.5 * atr_scale, c=c, v=10.0))
    e.warm(cs)
    return e


def test_pct_step_is_independent_of_timeframe():
    """ГЛАВНОЕ по этой правке: шаг в процентах одинаков на любом таймфрейме.
    Раньше шаг был grid_spacing × ATR, и переключение 1m→15m меняло ATR примерно
    в десять раз — вместе со всей лестницей."""
    thin = _warm(_engine(grid_step_mode="pct", grid_step_pct=1.0), atr_scale=1.0)
    wide = _warm(_engine(grid_step_mode="pct", grid_step_pct=1.0), atr_scale=10.0)
    assert wide.ind.atr > thin.ind.atr * 3, "контроль: ATR должен заметно отличаться"

    px = 100.0
    s1 = thin.quoter.step_size(px, thin.ind, thin.p)
    s2 = wide.quoter.step_size(px, wide.ind, wide.p)
    assert abs(s1 - s2) < 1e-9, f"шаг разъехался при разном ATR: {s1} vs {s2}"
    assert abs(s1 - 1.0) < 1e-9, f"шаг 1% от цены 100 должен быть 1.0, получено {s1}"


def test_atr_step_does_depend_on_timeframe():
    """Контроль обратного: режим 'atr' сохраняет прежнее поведение — шаг едет за ATR.
    Он оставлен переключателем, а не удалён."""
    thin = _warm(_engine(grid_step_mode="atr"), atr_scale=1.0)
    wide = _warm(_engine(grid_step_mode="atr"), atr_scale=10.0)
    s1 = thin.quoter.step_size(100.0, thin.ind, thin.p)
    s2 = wide.quoter.step_size(100.0, wide.ind, wide.p)
    assert s2 > s1 * 3, f"режим atr обязан следовать за ATR: {s1} vs {s2}"


def test_abs_step_is_exact_money():
    e = _warm(_engine(grid_step_mode="abs", grid_step_abs=100.0), atr_scale=5.0)
    assert abs(e.quoter.step_size(64000.0, e.ind, e.p) - 100.0) < 1e-9


def test_ladder_levels_use_fixed_pct_step():
    """Уровни реальной лестницы стоят ровно на шаге в % — проверяем сквозь движок."""
    e = _warm(_engine(grid_step_mode="pct", grid_step_pct=1.0, grid_side="neutral"),
              atr_scale=3.0)
    e.start_strategy(100.0)
    buys = sorted(o.price for o in e.orders if o.side == "buy" and not o.manual)
    assert len(buys) >= 2
    gap = buys[-1] - buys[-2]
    assert abs(gap - 1.0) < 1e-6, f"шаг между уровнями должен быть 1.0, получено {gap}"


# ─────────────────────────── состояние переживает рестарт ───────────────────────────
def test_ladder_state_survives_restart():
    """Центр и шаг лестницы обязаны сохраняться вместе с ордерами. Иначе после
    рестарта pair() возвращает None и филл перестаёт порождать парный take-profit —
    сетка молча вырождается в набор односторонних входов."""
    e = _engine(grid_side="long")
    e.start_strategy(e._last_price)
    st = e.to_state()
    assert st["ladder"]["installed"] and st["ladder"]["step"] > 0

    fresh = _engine(grid_side="long")
    fresh.load_state(st)
    assert fresh.ladder.installed, "лестница должна восстановиться как установленная"
    assert abs(fresh.ladder.step - e.ladder.step) < 1e-12
    assert abs(fresh.ladder.center - e.ladder.center) < 1e-12

    q = fresh.ladder.pair("buy", 100.0, 1.0)
    assert q is not None and q.side == "sell", "после рестарта парный TP должен строиться"
    assert abs(q.price - (100.0 + e.ladder.step)) < 1e-9


def test_pair_recovers_when_ladder_state_missing():
    """Страховка на сессию, сохранённую версией БЕЗ состояния лестницы: ордера
    восстановились, а центр и шаг — нет. Парная заявка обязана встать всё равно,
    иначе сетка вырождается в односторонний набор входов (позиция растёт, продаж нет)."""
    e = _engine(grid_side="long", grid_step_mode="pct", grid_step_pct=1.0)
    e.start_strategy(e._last_price)
    st = e.to_state()
    st.pop("ladder")                       # состояние из старого формата

    fresh = _engine(grid_side="long", grid_step_mode="pct", grid_step_pct=1.0)
    fresh.load_state(st)
    assert not fresh.ladder.installed, "контроль: лестница не должна восстановиться"

    fresh.active = True
    before = len(fresh.orders)
    fresh._place_pair("buy", 100.0, 1.0, ts=1)
    assert fresh.ladder.installed, "лестница должна восстановиться на лету"
    sells = [o for o in fresh.orders if o.side == "sell" and abs(o.price - 101.0) < 1e-9]
    assert sells, f"парный TP на 101.0 не выставлен; ордеров было {before}, стало {len(fresh.orders)}"


# ─────────────── метрики считаются ЧИСТЫМИ ───────────────
def test_trade_pnl_is_net_of_fees():
    """Round-trip в метриках обязан быть ЧИСТЫМ: минус комиссия входа и выхода.
    Иначе win rate и profit factor завышены ровно на величину издержек."""
    e = _engine()
    e._apply_fill("buy", 1.0, 100.0, is_maker=True, ts=1, note="")
    fee_in = e.pos.fees_paid
    assert e.pos.fees_open > 0, "комиссия входа должна остаться в позиции до закрытия"

    e._apply_fill("sell", 1.0, 101.0, is_maker=True, ts=2, note="")
    gross = 1.0 * (101.0 - 100.0)
    fee_out = e.pos.fees_paid - fee_in
    assert abs(e.pos.realized - gross) < 1e-9, "ценовой PnL должен остаться валовым"
    assert len(e.realized_pnls) == 1
    net = e.realized_pnls[0]
    assert abs(net - (gross - fee_in - fee_out)) < 1e-9, \
        f"в метрики должен идти чистый PnL {gross - fee_in - fee_out}, получено {net}"
    assert net < gross, "чистый результат обязан быть меньше валового"
    assert abs(e.pos.fees_open) < 1e-12, "после закрытия комиссия входа обнуляется"


def test_net_pnl_can_flip_a_marginal_win_to_loss():
    """Сделка, прибыль которой меньше комиссий, обязана считаться УБЫТОЧНОЙ."""
    e = _engine()
    e._apply_fill("buy", 1.0, 100.0, is_maker=True, ts=1, note="")
    e._apply_fill("sell", 1.0, 100.0001, is_maker=True, ts=2, note="")   # почти безубыток
    assert e.pos.realized > 0, "валовой PnL положителен"
    assert e.realized_pnls[0] < 0, "но после комиссий сделка убыточна"


def test_equity_convergence_unaffected_by_net_metrics():
    """Контроль: чистые метрики не должны сдвинуть эквити — оно считается по ценовому
    PnL и списанным комиссиям, а не по realized_pnls."""
    e = _engine()
    start = e.equity()
    e._apply_fill("buy", 1.0, 100.0, is_maker=True, ts=1, note="")
    e._apply_fill("sell", 1.0, 101.0, is_maker=True, ts=2, note="")
    assert abs(e.equity() - (start + e.pos.realized - e.pos.fees_paid)) < 1e-9


# ─────────────── биржевые ограничения инструмента ───────────────
def _spec_engine(alloc=1000.0, spec=None, **kw):
    p = GridParams(mode="grid", grid_side=kw.pop("grid_side", "long"), **kw)
    e = PaperEngine("BTCUSDT", alloc, p, CostModel.from_settings(Settings()),
                    funding={}, spec=spec)
    base, cs = 100.0, []
    for i in range(40):
        o = base + (i % 5) * 0.1
        c = o + (0.2 if i % 2 else -0.2)
        cs.append(Candle(ts=1_700_000_000_000 + i * 60_000,
                         o=o, h=o + 0.5, l=o - 0.5, c=c, v=10.0))
    e.warm(cs)
    return e


def test_price_rounds_to_tick_conservatively():
    """Цена заявки обязана лечь на шаг тика. Округляем консервативно: buy вниз,
    sell вверх — иначе заявка окажется БЛИЖЕ к рынку, чем задумано, и бумажный счёт
    получит исполнение по цене, до которой рынок в реальности не дошёл."""
    e = _spec_engine(spec={"tick_size": 0.5, "qty_step": 0.0, "min_qty": 0.0})
    assert e.round_price(100.37, "buy") == 100.0
    assert e.round_price(100.37, "sell") == 100.5
    assert e.round_price(100.5, "buy") == 100.5      # уже на тике — не двигаем
    assert e.round_price(100.5, "sell") == 100.5


def test_qty_rounds_down_to_lot_step():
    e = _spec_engine(spec={"tick_size": 0.0, "qty_step": 0.01, "min_qty": 0.0})
    assert abs(e.round_qty(1.2345) - 1.23) < 1e-12
    assert abs(e.round_qty(0.999) - 0.99) < 1e-12


def test_order_below_min_qty_is_not_placed():
    """Заявка меньше минимального объёма биржей отклоняется — значит и у нас
    её быть не должно, иначе бумажный счёт торгует то, чего не смог бы."""
    e = _spec_engine(spec={"tick_size": 0.0, "qty_step": 0.0, "min_qty": 5.0})
    assert e.round_qty(1.0) == 0.0
    e.active = True
    assert e._add_grid_order("buy", 99.0, 1.0, 1) is False
    assert e.rejected_min_qty == 1
    assert not e.orders


def test_ladder_respects_instrument_spec():
    """Сквозная проверка: все уровни реальной лестницы лежат на шаге тика."""
    e = _spec_engine(spec={"tick_size": 0.5, "qty_step": 0.001, "min_qty": 0.0},
                     grid_side="neutral", grid_step_mode="pct", grid_step_pct=1.0)
    e.start_strategy(100.0)
    assert e.orders
    for o in e.orders:
        k = o.price / 0.5
        assert abs(k - round(k)) < 1e-6, f"цена {o.price} не лежит на шаге тика 0.5"


def test_blocked_reason_survives_restart():
    """Причина блокировки обязана пережить рестарт.

    Иначе получается тихая ловушка: лестница сохранена как installed, ордеров
    ноль, _install_ladder после подъёма больше не вызывается — и в интерфейсе
    инструмент выглядит «активен · 0 орд», хотя биржа его заявки не примет
    никогда. Человек видит пустой график и не понимает, куда делась сетка."""
    e = _spec_engine(alloc=100.0, spec={"tick_size": 0.0, "qty_step": 0.001,
                                        "min_qty": 1.0})     # мин. лот заведомо неподъёмный
    e.start_strategy(100.0)
    assert e.blocked_reason, "контроль: инструмент обязан заблокироваться"
    assert not e.orders

    st = e.to_state()
    assert st["blocked_reason"], "причина не попала в состояние"

    fresh = _spec_engine(alloc=100.0, spec={"tick_size": 0.0, "qty_step": 0.001,
                                            "min_qty": 1.0})
    fresh.load_state(st)
    assert fresh.blocked_reason == e.blocked_reason, \
        "после рестарта инструмент выглядит рабочим, хотя торговать не может"
    assert fresh.rejected_min_qty == e.rejected_min_qty


# ─────────────── маржа под выставленные заявки ───────────────
def test_open_orders_consume_margin():
    """Заявка, способная нарастить позицию, занимает обеспечение — как на бирже."""
    e = _spec_engine(alloc=100.0)
    e._last_price = 100.0
    e.active = True
    assert e._margin_used(100.0) == 0.0
    e._add_grid_order("buy", 99.0, 1.0, 1)          # нотионал ~100 при плече 3 → маржа ~33
    used = e._margin_used(100.0)
    assert 32.0 < used < 34.0, f"ожидалась маржа ~33, получено {used}"


def test_netting_reserves_by_larger_side_not_sum():
    """Неттинг: встречные заявки не могут нарастить позицию одновременно, поэтому
    обеспечение считается по БОЛЬШЕЙ стороне. Сетка на $100 выставляет $100 покупок
    и $100 продаж — суммарно $200 нотионала, но реальный риск $100."""
    s = Settings()
    p = GridParams(mode="grid", grid_side="neutral", grid_levels=10,
                   grid_notional_mult=1.0, grid_step_mode="pct", grid_step_pct=1.0)
    cost = CostModel.from_settings(s)
    cost.leverage = 1.0                              # без плеча: маржа = нотионал
    e = PaperEngine("BTCUSDT", 100.0, p, cost, funding={})
    cs = [Candle(ts=1_700_000_000_000 + i * 60_000, o=100.0, h=100.5, l=99.5,
                 c=100.0 + (0.2 if i % 2 else -0.2), v=10.0) for i in range(40)]
    e.warm(cs)
    e.start_strategy(100.0)
    buys = sum(o.size * o.price for o in e.orders if o.side == "buy" and not o.manual)
    sells = sum(o.size * o.price for o in e.orders if o.side == "sell" and not o.manual)
    assert buys > 0 and sells > 0, "должны стоять обе стороны"
    assert max(buys, sells) <= 100.0 + 1e-6, \
        f"худшая сторона ${max(buys, sells):.2f} превысила капитал $100"
    assert e._margin_used(100.0) <= 100.0 + 1e-6


def test_margin_rejects_orders_beyond_capital():
    """Запрос нотионала выше доступного обеспечения: часть уровней не выставится —
    ровно как отклонила бы биржа."""
    s = Settings()
    p = GridParams(mode="grid", grid_side="long", grid_levels=10,
                   grid_notional_mult=4.0,           # заведомо больше, чем позволит плечо 1
                   grid_step_mode="pct", grid_step_pct=1.0)
    cost = CostModel.from_settings(s)
    cost.leverage = 1.0
    e = PaperEngine("BTCUSDT", 100.0, p, cost, funding={})
    cs = [Candle(ts=1_700_000_000_000 + i * 60_000, o=100.0, h=100.5, l=99.5,
                 c=100.0 + (0.2 if i % 2 else -0.2), v=10.0) for i in range(40)]
    e.warm(cs)
    e.start_strategy(100.0)
    buys = sum(o.size * o.price for o in e.orders if o.side == "buy")
    assert buys <= 100.0 + 1e-6, f"нотионал покупок ${buys:.2f} превысил капитал $100"
    assert e.rejected_margin > 0, "часть заявок должна быть отклонена по марже"


# ─────────────── ликвидация по mark price ───────────────
def test_liquidation_uses_mark_price_not_last():
    """Bybit ликвидирует по mark price. Если last дёрнулся вниз на тонком стакане,
    а mark остался на месте, позицию трогать нельзя."""
    def run(mark):
        e = _spec_engine(alloc=1000.0)
        e.active = True
        px = 100.0
        e.pos.qty = (1000.0 / px) * 30.0             # заведомо большое плечо
        e.pos.avg_entry = px
        e.mark_price = mark
        e._check_liquidation(ts=1, mark=50.0)        # last провалился вдвое
        return e.liquidated

    assert run(0.0) is True, "без mark price ликвидация считается по last — контроль"
    assert run(100.0) is False, "при неизменной mark price ликвидации быть не должно"
    assert run(50.0) is True, "если mark тоже упала — ликвидация обязана сработать"


# ─────────────────────────── сайзинг и плечо ───────────────────────────
def test_order_size_scales_with_allocation():
    """Размер ордера обязан зависеть от аллокации инструмента. Раньше стояла
    константа order_usd=80, и при alloc=$100 (корзина из 10) сетка набирала
    позицию в 8 раз больше обеспечивающего её капитала."""
    big = _engine(alloc=1000.0)
    small = _engine(alloc=100.0)
    px = 100.0
    assert abs(big._grid_size_of(px) / small._grid_size_of(px) - 10.0) < 1e-9, \
        "размер ордера должен масштабироваться пропорционально аллокации"


def test_leverage_cap_is_enforced():
    """Плечо из .env теперь действительно ограничивает позицию. Проверяем, что
    потолок нотионала не превышает alloc × leverage."""
    s = Settings()
    e = _engine(alloc=100.0, grid_notional_mult=50.0)   # заведомо абсурдный запрос
    px = 100.0
    cap_notional = e._max_contracts(px) * px
    assert cap_notional <= 100.0 * s.leverage + 1e-6, \
        f"потолок ${cap_notional:.2f} превысил alloc×leverage=${100.0 * s.leverage:.2f}"


def test_notional_mult_limits_inventory():
    """grid_notional_mult=1.0 → инвентарь не превышает аллокацию (без плеча)."""
    e = _engine(alloc=1000.0, grid_notional_mult=1.0)
    e.active = True
    px = e._last_price
    for i in range(400):
        px *= 0.999                       # затяжной дрейф вниз — сетка набирает лонг
        e.on_tick(px, 1_700_000_000_000 + (100 + i) * 60_000)
    notional = abs(e.pos.qty) * e._last_price
    assert notional <= 1000.0 * 1.02, \
        f"инвентарь ${notional:.2f} превысил аллокацию $1000 при mult=1.0"


# ─────────────────────────── ликвидация терминальна ───────────────────────────
def test_liquidation_is_terminal():
    """Ликвидация происходит ОДИН раз и останавливает счёт. До правки флаг ставился,
    но _on_price_event переустанавливал сетку, не сверяясь с ним: на реальных данных
    один инструмент ликвидировался 410 раз за 1000 баров."""
    e = _engine(alloc=1000.0)
    e.active = True
    px = e._last_price
    e.pos.qty = (1000.0 / px) * 30.0      # навязанное плечо → неминуемая ликвидация
    e.pos.avg_entry = px
    p = px
    for i in range(60):
        p *= 0.99
        e.on_tick(p, 1_700_000_000_000 + (100 + i) * 60_000)

    liq = [ev for ev in e.events if "Ликвид" in ev.action]
    assert len(liq) == 1, f"ликвидация должна быть ровно одна, произошло {len(liq)}"
    assert e.liquidated and not e.active, "после ликвидации торговля должна прекратиться"
    assert not any(not o.manual for o in e.orders), "сеточные заявки должны быть сняты"


def test_equity_never_below_zero_after_liquidation():
    """Изолированная маржа: потеря ограничена аллокацией. Отрицательного эквити
    быть не может — раньше счёт уходил до −$94 при аллокации $68."""
    e = _engine(alloc=1000.0)
    e.active = True
    px = e._last_price
    e.pos.qty = (1000.0 / px) * 50.0
    e.pos.avg_entry = px
    p = px
    for i in range(80):
        p *= 0.98
        e.on_tick(p, 1_700_000_000_000 + (100 + i) * 60_000)
    assert e.equity() >= 0.0, f"эквити ушло ниже нуля: {e.equity():.2f}"
    assert e.cash >= 0.0, f"баланс ушёл ниже нуля: {e.cash:.2f}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (classic grid + risk)")
