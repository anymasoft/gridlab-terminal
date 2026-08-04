"""Классический сеточный режим: НЕПОДВИЖНАЯ лестница уровней + парный take-profit.

Чем это отличается от режима `avellaneda` (маркет-мейкинг), который был дефолтным:
там после каждого исполнения котировки ПЕРЕЦЕНТРИРУЮТСЯ на текущую цену. Сетка
гонится за ценой: в растущем тренде она покупает всё выше и выше и никогда не
получает парную продажу на уровень выше входа. На корзине из 10 перпов такая
перецентровка давала 8 939 сделок за 1000 баров при почти нулевом эдже на сделку —
оборот съедал счёт.

Классический грид устроен наоборот и на этом зарабатывает:

    уровни СТОЯТ НЕПОДВИЖНО;
    лот, купленный на уровне L, продаётся лимиткой ровно на L + step;
    исполненная продажа возвращает заявку на покупку обратно на L.

Прибыль снимается с колебания цены между уровнями, а не с угадывания направления.
Каждый round-trip приносит ровно `step × qty` минус две maker-комиссии — поэтому
шаг сетки должен быть заметно больше комиссии (при maker 1 bp и шаге ~1 ATR
комиссия составляет единицы процентов от прибыли сделки).

Правило парности одно и то же для обеих сторон, поэтому режимы long и neutral
отличаются только начальной установкой:

    buy  исполнен на P  →  выставить sell на P + step
    sell исполнен на P  →  выставить buy  на P − step

Что этот режим НЕ делает: не защищает от тренда. Если цена уходит вниз и не
возвращается, лестница набирает инвентарь до потолка и держит нереализованный
убыток. Грид «занимает» прибыль у колебаний и отдаёт её тренду — потолок позиции
(alloc × grid_notional_mult) и есть весь риск-контроль этого режима.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class LadderQuote:
    side: str       # 'buy' | 'sell'
    price: float
    size: float     # qty в контрактах
    level: int      # номер уровня от центра (для метки G1..Gn)


class GridLadder:
    """Состояние неподвижной лестницы. Владеет центром и шагом; набор открытых
    заявок живёт в движке — здесь только правило, где им быть."""

    def __init__(self, n_levels: int = 10, side: str = "long") -> None:
        self.n = max(1, int(n_levels))
        self.side = side if side in ("long", "neutral") else "long"
        self.center = 0.0
        self.step = 0.0
        self.installed = False

    # ───────── установка ─────────
    def install(self, center: float, step: float,
                size_of: Callable[[float], float]) -> list[LadderQuote]:
        """Построить лестницу вокруг center. Вызывается ОДИН раз за жизнь сетки
        (и ещё раз при ре-анкоре после пробоя) — не на каждом исполнении."""
        if center <= 0 or step <= 0:
            return []
        self.center = center
        self.step = step
        self.installed = True
        out: list[LadderQuote] = []
        for i in range(1, self.n + 1):
            bp = center - i * step
            if bp > 0:
                out.append(LadderQuote("buy", bp, size_of(bp), i))
            if self.side == "neutral":
                sp = center + i * step
                out.append(LadderQuote("sell", sp, size_of(sp), i))
        return out

    # ───────── парная заявка ─────────
    def pair(self, side: str, price: float, qty: float) -> LadderQuote | None:
        """Заявка, которую надо выставить после исполнения на цене price.
        Это и есть механизм съёма прибыли: round-trip = ровно один шаг сетки."""
        if not self.installed or self.step <= 0 or qty <= 0:
            return None
        if side == "buy":
            tp = price + self.step
            return LadderQuote("sell", tp, qty, self.level_of(tp))
        bp = price - self.step
        if bp <= 0:
            return None
        return LadderQuote("buy", bp, qty, self.level_of(bp))

    def level_of(self, price: float) -> int:
        if self.step <= 0:
            return 0
        return int(round(abs(price - self.center) / self.step))

    # ───────── пробой диапазона ─────────
    def out_of_range(self, price: float, span_mult: float) -> bool:
        """Цена ушла за пределы лестницы больше чем на span_mult её диапазонов →
        сетка простаивает, имеет смысл переставить её вокруг новой цены.

        Выключено по умолчанию (span_mult=0): ре-анкор фиксирует нереализованный
        убыток набранного инвентаря и на исторических прогонах ухудшал результат.
        Оставлено параметром, а не зашито, — это предмет исследования, не догма."""
        if not self.installed or span_mult <= 0 or self.step <= 0:
            return False
        span = self.n * self.step
        return abs(price - self.center) > span * (1.0 + span_mult)

    def reset(self) -> None:
        self.installed = False
        self.center = 0.0
        self.step = 0.0

    def info(self) -> dict:
        return {"center": round(self.center, 6) if self.center else None,
                "step": round(self.step, 6) if self.step else None,
                "levels": self.n, "side": self.side}
