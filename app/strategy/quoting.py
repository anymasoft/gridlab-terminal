"""Два режима квотирования адаптивной сетки. Оба возвращают ЖЕЛАЕМЫЙ набор
лимитных уровней (buy ниже, sell выше) вокруг цены — движок их сверяет с
открытыми ордерами и доставляет/снимает.

ОДНА стратегия (адаптивный grid), но два механизма адаптации для сравнения:

1) heuristic — идея владельца: «чем хуже позиция/чем больше инвентарь, тем шире
   шаг и осторожнее»; шаг = k×ATR с расширением по уровням; EMA-фильтр тренда.
2) avellaneda — КАНОНИЧЕСКИЙ Avellaneda-Stoikov (модуль avellaneda_canonical):
   reservation price r = s − q·γ·σ²·T, полный спред δ из σ/γ/κ; ОДНА пара bid/ask
   (осознанно, без сетки); σ — из доходностей (не ATR-прокси).
3) avellaneda_legacy — ПРЕЖНЯЯ самоделка (ATR-нормировка), задепрекейчена; оставлена
   для сравнения (диагностика: не A-S, дистанция котировок выходила 1.3–2.4 ATR).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import GridParams, Position
from . import avellaneda_canonical as ac
from .indicators import Indicators


@dataclass
class QuoteLevel:
    side: str       # 'buy' | 'sell'
    price: float
    size: float     # qty в контрактах
    level: int      # 1..N для метки G1..Gn


def _expansion_factors(n: int, kind: str) -> list[float]:
    """Множители шага по уровням (1-й уровень ближе всего к цене)."""
    if kind == "arithmetic":
        return [float(i + 1) for i in range(n)]
    if kind == "fibonacci":
        a, b, out = 1.0, 1.0, []
        for _ in range(n):
            out.append(a)
            a, b = b, a + b
        return out
    # geometric
    r = 1.6
    return [r ** i for i in range(n)]


def desired_quotes(price: float, ind: Indicators, pos: Position,
                   p: GridParams, alloc: float) -> list[QuoteLevel]:
    if price <= 0 or ind.atr <= 0:
        return []
    if p.mode == "avellaneda":
        return _avellaneda_canonical(price, ind, pos, p, alloc)
    if p.mode == "avellaneda_legacy":
        return _avellaneda_legacy(price, ind, pos, p, alloc)
    return _heuristic(price, ind, pos, p, alloc)


def _order_qty(notional_usd: float, price: float) -> float:
    return notional_usd / price if price > 0 else 0.0


def _size(p: GridParams, price: float) -> float:
    """Размер ордера: фьючерс — фикс. число контрактов (contract_qty), иначе нотионал в $."""
    if p.contract_qty > 0:
        return p.contract_qty
    return _order_qty(p.order_usd, price)


def _heuristic(price: float, ind: Indicators, pos: Position,
               p: GridParams, alloc: float) -> list[QuoteLevel]:
    atr = ind.atr_smooth or ind.atr      # сглаженный шаг — сетка не «дёргается»
    base_step = max(p.grid_spacing, 0.05) * atr
    factors = _expansion_factors(1, p.expansion)

    # инвентарь как доля капитала (>0 лонг). Чем больше — тем осторожнее с добором.
    inv_ratio = abs(pos.qty) * price / alloc if alloc > 0 else 0.0
    inv_widen = 1.0 + min(inv_ratio, 2.0)          # шаг на стороне набора расширяется
    long_inv = pos.qty > 0

    # EMA-фильтр тренда: в нисходящем тренде покупки осторожнее, продажи агрессивнее
    downtrend = price < ind.ema
    buy_skew = 1.35 if downtrend else 0.9          # >1 = шире (осторожнее)
    sell_skew = 0.9 if downtrend else 1.35

    # потолок инвентаря: при максимуме на стороне — не добавляем новые на этой стороне
    if p.contract_qty > 0:
        inv_units = abs(pos.qty) / p.contract_qty
    else:
        inv_units = abs(pos.qty) * price / max(p.order_usd, 1e-9)
    block_buy = long_inv and inv_units >= p.max_orders
    block_sell = (pos.qty < 0) and inv_units >= p.max_orders

    out: list[QuoteLevel] = []
    cum = 0.0
    for i, f in enumerate(factors):
        cum += base_step * f
        qty = _size(p, price)
        # buy ниже
        if not block_buy:
            w = inv_widen if long_inv else 1.0
            bp = price - cum * buy_skew * w
            if bp > 0:
                out.append(QuoteLevel("buy", bp, qty, i + 1))
        # sell выше
        if not block_sell:
            w = inv_widen if pos.qty < 0 else 1.0
            sp = price + cum * sell_skew * w
            out.append(QuoteLevel("sell", sp, qty, i + 1))
    return out[: p.max_orders]


def _avellaneda_canonical(price: float, ind: Indicators, pos: Position,
                          p: GridParams, alloc: float) -> list[QuoteLevel]:
    """Канонический A-S (модуль avellaneda_canonical): ОДНА пара bid/ask вокруг
    reservation price. σ — волатильность доходностей (ind.sigma), НЕ ATR-прокси;
    центр смещается против инвентаря; полный спред из σ/γ/κ/горизонта. Формулы,
    единицы и честная оговорка — в avellaneda_canonical.py."""
    sigma = max(ind.sigma, 1e-6)               # σ доходностей за бар (доля), не ATR
    gamma = max(p.as_gamma, 1e-3)
    kappa = max(p.as_kappa, 1e-3)
    T = max(p.as_horizon, 1)

    # инвентарь как БЕЗРАЗМЕРНАЯ знаковая доля капитала (>0 лонг)
    q = (pos.qty * price) / alloc if alloc > 0 else 0.0
    asq = ac.quote(price, q, gamma, sigma, kappa, T)
    qty = _size(p, price)

    # потолок инвентаря (как в др. режимах): не добирать сторону сверх max_orders
    if p.contract_qty > 0:
        inv_units = abs(pos.qty) / p.contract_qty
    else:
        inv_units = abs(pos.qty) * price / max(p.order_usd, 1e-9)
    block_buy = pos.qty > 0 and inv_units >= p.max_orders
    block_sell = pos.qty < 0 and inv_units >= p.max_orders

    out: list[QuoteLevel] = []
    if not block_buy and asq.bid > 0:
        out.append(QuoteLevel("buy", asq.bid, qty, 1))
    if not block_sell and asq.ask > 0:
        out.append(QuoteLevel("sell", asq.ask, qty, 1))
    return out


def _avellaneda_legacy(price: float, ind: Indicators, pos: Position,
                       p: GridParams, alloc: float) -> list[QuoteLevel]:
    """ЗАДЕПРЕКЕЙЧЕНО (оставлено для сравнения, режим 'avellaneda_legacy'). Прежняя
    самоделка: A-S, привязанный к ATR с эмпирическими коэффициентами; диагностика
    (REPORT_GRIDLAB_DIAGNOSIS.md) показала, что это НЕ канонический A-S — дистанция
    выходила 1.3–2.4 ATR. Канон — в _avellaneda_canonical / avellaneda_canonical.py."""
    atr = ind.atr_smooth or ind.atr        # сглаженный шаг — сетка не «дёргается»
    sigma = max(ind.sigma, 1e-5)            # σ доходности за бар (доля)
    gamma = max(p.as_gamma, 1e-3)
    kappa = max(p.as_kappa, 1e-3)
    T = max(p.as_horizon, 1)

    # инвентарь нормируем долей капитала (знаковый): >0 лонг
    q = (pos.qty * price) / alloc if alloc > 0 else 0.0
    sigma_px = sigma * price                # волатильность в цене за бар

    # reservation price: центр смещается ОТ mid против инвентаря (ATR-нормировка)
    skew = q * gamma * atr * 2.0 + q * gamma * sigma_px * math.sqrt(T) * 0.05
    r = price - skew

    # оптимальный полуспред: база ATR × риск-аверсия + надбавка за поток (ln) + σ-член
    flow = (1.0 / gamma) * math.log(1 + gamma / kappa)          # ~0.2..1.0
    half_spread = atr * (0.5 * p.grid_spacing) * (1 + 0.6 * gamma) \
        + 0.25 * flow * atr + 0.04 * sigma_px * math.sqrt(T)
    half_spread = max(half_spread, atr * 0.2)

    factors = _expansion_factors(1, p.expansion)
    qty = _size(p, price)

    # потолок инвентаря (как в эвристике): набрано max_orders в одну сторону → не ставим лимитки на набор
    if p.contract_qty > 0:
        inv_units = abs(pos.qty) / p.contract_qty
    else:
        inv_units = abs(pos.qty) * price / max(p.order_usd, 1e-9)
    block_buy = pos.qty > 0 and inv_units >= p.max_orders
    block_sell = pos.qty < 0 and inv_units >= p.max_orders

    out: list[QuoteLevel] = []
    for i, f in enumerate(factors):
        off = half_spread * (1 + (f - 1) * 0.6)   # лесенка вокруг reservation price
        bp = r - off
        sp = r + off
        if not block_buy and bp > 0:
            out.append(QuoteLevel("buy", bp, qty, i + 1))
        if not block_sell:
            out.append(QuoteLevel("sell", sp, qty, i + 1))
    return out[: p.max_orders]
