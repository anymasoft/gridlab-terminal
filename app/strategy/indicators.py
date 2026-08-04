"""Индикаторы для адаптивной сетки: ATR, EMA, σ доходностей. Инкрементальные,
чтобы дёшево гонять по тысячам баров в бэктесте."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import Candle


@dataclass
class Indicators:
    """Состояние индикаторов, обновляемое по одному бару (Wilder ATR, EMA, rolling σ)."""
    atr_window: int = 14
    ema_window: int = 21
    sigma_window: int = 30

    atr: float = 0.0
    atr_smooth: float = 0.0     # EMA-сглаженный ATR — стабильный шаг сетки (не «дёргается»)
    ema: float = 0.0
    sigma: float = 0.0          # σ лог-доходностей за окно (доля)
    atr_smooth_alpha: float = 0.1
    _prev_close: float = 0.0
    _seeded: bool = False
    _rets: list[float] = field(default_factory=list)
    _tr_sum: float = 0.0
    _tr_count: int = 0

    def update(self, c: Candle) -> None:
        # True Range
        if self._seeded:
            tr = max(c.h - c.l, abs(c.h - self._prev_close), abs(c.l - self._prev_close))
        else:
            tr = c.h - c.l
        # Wilder smoothing
        if not self._seeded:
            self.atr = tr
            self.ema = c.c
        else:
            if self._tr_count < self.atr_window:
                self._tr_sum += tr
                self._tr_count += 1
                self.atr = self._tr_sum / self._tr_count
            else:
                self.atr = (self.atr * (self.atr_window - 1) + tr) / self.atr_window
            k = 2.0 / (self.ema_window + 1)
            self.ema = c.c * k + self.ema * (1 - k)
            # лог-доходность для σ
            if self._prev_close > 0:
                self._rets.append(math.log(c.c / self._prev_close))
                if len(self._rets) > self.sigma_window:
                    self._rets.pop(0)
                if len(self._rets) >= 2:
                    m = sum(self._rets) / len(self._rets)
                    var = sum((x - m) ** 2 for x in self._rets) / (len(self._rets) - 1)
                    self.sigma = math.sqrt(var)
        # сглаженный ATR (раз на бар в обоих путях — Live и бэктест → одинаковый шаг)
        if self.atr_smooth <= 0:
            self.atr_smooth = self.atr
        else:
            self.atr_smooth += self.atr_smooth_alpha * (self.atr - self.atr_smooth)
        self._prev_close = c.c
        self._seeded = True

    @property
    def ready(self) -> bool:
        return self._tr_count >= min(self.atr_window, 5)
