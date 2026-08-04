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
        """Расстояние между уровнями.

        В режиме 'grid' шаг по умолчанию задаётся в ПРОЦЕНТАХ от цены и потому не
        зависит от таймфрейма графика: 1% — это 1% и на минутках, и на часах. Прежний
        режим 'atr' (шаг = grid_spacing × ATR) оставлен переключателем: он адаптируется
        к волатильности, но привязывает стратегию к выбранному масштабу — переключение
        1m→15m меняло ATR примерно в десять раз и перестраивало всю лестницу.

        Режимы маркет-мейкинга (avellaneda/heuristic) всегда считают шаг от ATR:
        там дистанция котировок — часть модели, а не пользовательская настройка."""
        atr = (ind.atr_smooth or ind.atr) or center * 0.005
        if p.mode == "grid":
            if p.grid_step_mode == "abs" and p.grid_step_abs > 0:
                self.last_step = p.grid_step_abs
            elif p.grid_step_mode == "pct" and p.grid_step_pct > 0:
                self.last_step = center * p.grid_step_pct / 100.0
            else:
                self.last_step = max(p.grid_spacing, 0.1) * atr
            return self.last_step
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
