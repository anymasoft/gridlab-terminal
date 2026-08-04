"""Quoter — ЕДИНАЯ политика котирования адаптивной сетки.

Корень прежних багов: Live (on_tick) и бэктест (step) строили сетку РАЗНЫМ кодом —
Live наивно (1 buy + 1 sell, фикс. шаг), бэктест через desired_quotes (многоуровневая
модель с inventory-skew). Теперь ОБА пути зовут один и тот же Quoter.build(): по
состоянию рынка/позиции он возвращает желаемый набор лимиток. Матчинг/учёт остаётся
в PaperEngine, котирование — здесь. Один код → Live и бэктест дают одинаковую сетку
при одинаковых параметрах.

Что делает Quoter поверх desired_quotes:
- многоуровневая сетка с расширением (geometric/arith/fib) и инвентарным скосом
  (reservation price смещается против позиции; шаг на стороне набора расширяется);
- стабильный шаг: основан на сглаженном ATR (ind.atr_smooth), не на мгновенном —
  сетка не «дёргается» при всплеске волатильности;
- клэмп: ни один уровень не пересекает центр (иначе мгновенное исполнение).
"""
from __future__ import annotations

from ..models import GridParams, Position
from .indicators import Indicators
from .quoting import QuoteLevel, desired_quotes


class Quoter:
    def __init__(self) -> None:
        self.last_step: float = 0.0   # реальный шаг последней построенной сетки (для панели)

    def step_size(self, center: float, ind: Indicators, p: GridParams) -> float:
        atr = (ind.atr_smooth or ind.atr) or center * 0.005
        self.last_step = max(p.grid_spacing, 0.1) * atr
        return self.last_step

    def build(self, center: float, ind: Indicators, pos: Position,
              p: GridParams, alloc: float) -> list[QuoteLevel]:
        """Желаемый набор лимиток вокруг center. Пусто, если рынок не прогрет."""
        if center <= 0 or not ind.ready:
            return []
        step = self.step_size(center, ind, p)
        quotes = desired_quotes(center, ind, pos, p, alloc)
        out: list[QuoteLevel] = []
        for q in quotes:
            if q.side == "buy":
                px = min(q.price, center - 0.1 * step)   # строго ниже центра
                if px > 0:
                    out.append(QuoteLevel("buy", px, q.size, q.level))
            else:
                px = max(q.price, center + 0.1 * step)   # строго выше центра
                out.append(QuoteLevel("sell", px, q.size, q.level))
        return out[: p.max_orders]
