"""Бумажный матчинг-движок одного инструмента — максимально близко к Bybit.

Учитывает обе ноги издержек: maker-комиссия на исполнении grid-лимиток, taker+
проскальзывание на market-закрытии (SL/ликвидация), funding каждые 8ч, плечо,
маржу и ликвидацию, частичное исполнение по ликвидности бара. Реальные свечи Bybit.

Шаговый (step) для live/replay и батчевый (run) для бэктеста — одно состояние.
"""
from __future__ import annotations

import datetime
import math
from collections import deque
from dataclasses import dataclass, field

from ..models import Candle, Fill, GridParams, Position
from ..strategy.indicators import Indicators
from ..strategy.ladder import GridLadder
from ..strategy.quoter import Quoter
from .costmodel import CostModel

# Торговая сессия MOEX (Si/Eu/ED/RTS): 10:00–23:50 МСК
_SESSION_START = datetime.time(10, 0)
_SESSION_END = datetime.time(23, 50)
_MSK = datetime.timezone(datetime.timedelta(hours=3))


@dataclass
class _OpenOrder:
    side: str
    price: float
    size: float
    level: int
    manual: bool = False     # ручной ордер (поставлен мышью), не трогается логикой грида
    oid: int = 0
    queue_ahead: float = 0.0  # ОЦЕНКА объёма перед нами на уровне (live: из книги; тает по сделкам)


@dataclass
class StepEvent:
    ts: int
    action: str
    price: float
    size: float
    fee: float
    pnl: float
    comment: str


# сколько последних закрытых пар держать в памяти и в состоянии сессии
_ROUNDTRIP_CAP = 500


class PaperEngine:
    def __init__(self, sym: str, alloc: float, params: GridParams, cost: CostModel,
                 funding: dict[int, float] | None = None, mult: float = 1.0,
                 spec: dict | None = None):
        self.sym = sym
        self.alloc = alloc
        self.p = params
        self.cost = cost
        self.funding = funding or {}
        # Биржевые ограничения инструмента: шаг цены, шаг лота, минимальный объём.
        # Без них бумажный счёт выставляет заявки, которых биржа бы не приняла:
        # цена не по тику, объём не по шагу лота, объём ниже минимального.
        spec = spec or {}
        self.tick_size = float(spec.get("tick_size") or 0.0)
        self.qty_step = float(spec.get("qty_step") or 0.0)
        self.min_qty = float(spec.get("min_qty") or 0.0)
        # Предельное плечо ИНСТРУМЕНТА (Bybit: BTC 100x, BNB 50x, DOGE 75x).
        # 0 = биржа не ответила, тогда ограничиваем только конфигом.
        self.max_leverage = float(spec.get("max_leverage") or 0.0)
        # Mark price биржи. Ликвидация у Bybit считается ОТ НЕЁ, а не от последней сделки:
        # last может дёрнуться на тонком стакане, mark сглажен по индексу.
        self.mark_price = 0.0
        self.rejected_min_qty = 0    # сколько заявок не выставлено: объём ниже минимума
        self.rejected_margin = 0     # сколько заявок не выставлено: не хватило маржи
        self.blocked_reason = ""     # почему инструмент не торгуется (пусто = торгуется)
        # mult — денежная стоимость 1 пункта цены за 1 контракт (₽). Крипто-режим: 1.0
        # (цена уже в деньгах). Фьючерс: point_value из спеки → PnL считается в рублях.
        self.mult = mult
        self.ind = Indicators(atr_window=params.atr_window, ema_window=params.ema)
        self.quoter = Quoter()    # единая политика котирования (Live == бэктест)
        # Классический грид: неподвижная лестница уровней с парным take-profit.
        # Используется только в режиме 'grid'; в MM-режимах котирует Quoter.
        self.ladder = GridLadder(params.grid_levels, params.grid_side)
        self.pos = Position()
        self.cash = alloc
        self.orders: list[_OpenOrder] = []
        self.equity_curve: list[float] = []
        self.equity_curve_live: list[float] = []   # realtime-эквити (тиковый режим)
        self.events: list[StepEvent] = []
        self.fills: list[Fill] = []
        self.trades = 0           # число round-trip реализаций
        self.buy_filled = 0.0     # суммарно исполнено контрактов в ЛОНГ (покупки)
        self.sell_filled = 0.0    # суммарно исполнено контрактов в ШОРТ (продажи)
        self.realized_pnls: list[float] = []
        # Закрытые пары «вход → выход» с деньгами. Хранится ограниченный хвост:
        # это журнал для чтения человеком, а не источник метрик.
        self.roundtrips: list[dict] = []
        self.last_funding_ts = 0
        self.liquidated = False
        self.active = False       # стратегия запущена явно? до этого — только ручные ордера
        self._uid = 0
        self._last_price = 0.0
        # ── блок D: единый каденс + честный trade-through ──
        self.trade_driven = False                # филлы из РЕАЛЬНЫХ сделок (process_trade), а не тика
        self.ob_book: dict | None = None         # снимок живой книги для оценки очереди (live, выбранный символ)
        self._quote_anchor = (0.0, 0.0, 0.0)     # (mid, σ, инвентарь) на момент котирования — порог переоценки
        self.requote_frac = 1.0                  # переоценка при сдвиге mid >= шага сетки (заявка
                                                 # успевает «дождаться» цены раньше, чем убежит)
        self.requote_inv = 0.25                  # ... или при изменении инвентаря (доля капитала)
        self.requote_sigma = 0.5                 # ... или при относительном изменении σ
        # ── блок F: метрики MM (разложение PnL спред/инвентарь, fill-rate, markout, инвентарь) ──
        self.mm_spread_px = 0.0      # Σ signed·(mid_at_fill − price) в ЦЕНЕ (×mult при отчёте) — спред-доход (мейкер-эдж)
        self.mm_spread_n = 0         # по скольким филлам была ИЗВЕСТНА середина книги.
                                     # 0 → разложение спред/инвентарь недостоверно (нет L2)
        self.mm_posted = 0           # выставлено сеточных заявок (для fill-rate)
        self.mm_filled = 0           # исполнено сеточных заявок
        self.inv_curve: list[float] = []   # инвентарь во времени
        self.mk_horizon_ms = 60_000  # горизонт markout (adverse selection): цена через 60с после филла
        self.mk_sum = 0.0            # Σ markout (доля), знак в пользу мейкера
        self.mk_n = 0
        self.mk_adverse = 0          # сколько филлов с markout<0
        self._mk_pending: deque = deque()  # (ts, fill_price, side) — ждут горизонта
        # ── блок G: риск-контроль ──
        self.peak_equity = alloc     # пик эквити — база для kill-switch по просадке
        self.halted = False          # kill-switch сработал → котирование остановлено

    # ───────── позиция (неттинг) ─────────
    def _apply_fill(self, side: str, qty: float, price: float, is_maker: bool,
                    ts: int, note: str, mid: float | None = None) -> float:
        """Применить исполнение. Возвращает реализованный ЦЕНОВОЙ PnL этой сделки.

        mid — справедливая середина книги на момент филла. Передаётся ТОЛЬКО когда она
        реально известна (live, есть L2-книга). В бэктесте книги нет, и подставлять
        вместо неё цену пробившего тика нельзя: для buy-лимитки такой «mid» всегда
        не выше цены заявки, и спред-доход выходит структурно отрицательным. Поэтому
        при mid=None разложение просто не накапливается — см. mm_metrics."""
        signed = qty if side == "buy" else -qty
        if mid is not None:
            self.mm_spread_px += signed * (mid - price)   # мейкер купил ниже / продал выше mid
            self.mm_spread_n += 1
        notional = abs(qty) * price
        fee = self.cost.fee(notional, is_maker) * self.mult   # ×mult → деньги (₽ для фьючерса)
        realized = 0.0
        pos = self.pos
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            # открытие/добор в ту же сторону → пересчёт средней
            new_qty = pos.qty + signed
            if new_qty != 0:
                pos.avg_entry = (pos.avg_entry * pos.qty + price * signed) / new_qty
            pos.qty = new_qty
            pos.fees_open += fee            # комиссия входа — «сидит» в позиции до закрытия
        else:
            # сокращение/разворот
            closing = min(abs(signed), abs(pos.qty))
            realized = closing * (price - pos.avg_entry) * (1 if pos.qty > 0 else -1) * self.mult
            pos.realized += realized        # ЦЕНОВОЙ PnL, без комиссий — на нём держится
            self.cash += realized           # сходимость эквити, трогать нельзя
            self.trades += 1
            # ЧИСТЫЙ результат round-trip: минус доля комиссии входа, приходящаяся на
            # закрытый объём, и минус комиссия выхода за тот же объём. Именно он идёт
            # в win rate / profit factor — иначе метрики завышены на величину издержек.
            entry_fee_share = pos.fees_open * (closing / abs(pos.qty)) if pos.qty else 0.0
            exit_fee_share = fee * (closing / abs(qty)) if qty else 0.0
            pos.fees_open -= entry_fee_share
            net = realized - entry_fee_share - exit_fee_share
            self.realized_pnls.append(net)
            # Журнал ЗАКРЫТЫХ ПАР. Список филлов на этот вопрос не отвечает:
            # в нём видно «купил» и «продал» по отдельности, а человеку нужно
            # «взял по 1000, отдал по 1200, заработал 200». Пишем сразу обе ноги.
            self.roundtrips.append({
                "ts": ts,
                "dir": "long" if pos.qty > 0 else "short",
                "entry": pos.avg_entry,
                "exit": price,
                "qty": closing,
                "gross": realized,
                "fee": entry_fee_share + exit_fee_share,
                "net": net,
            })
            if len(self.roundtrips) > _ROUNDTRIP_CAP:
                del self.roundtrips[:len(self.roundtrips) - _ROUNDTRIP_CAP]
            remaining = abs(signed) - closing
            pos.qty += signed
            if abs(pos.qty) < 1e-12:
                pos.qty = 0.0
                pos.avg_entry = 0.0
                pos.fees_open = 0.0
            if remaining > 1e-12:
                # разворот: старая позиция закрыта ПОЛНОСТЬЮ, остаток открывает новую
                # позицию по ЦЕНЕ СДЕЛКИ → avg_entry сбрасывается на price (не остаётся старым)
                pos.qty = remaining if signed > 0 else -remaining
                pos.avg_entry = price
                pos.fees_open = fee - exit_fee_share   # остаток комиссии — вход новой позиции
        self.cash -= fee
        pos.fees_paid += fee
        if signed > 0:
            self.buy_filled += abs(qty)
        else:
            self.sell_filled += abs(qty)
        self.fills.append(Fill(self._uid, side, price, qty, fee, ts, is_maker, note))
        return realized

    # ───────── funding ─────────
    def _funding_rate(self, ts: int) -> float:
        if not self.funding:
            return self.cost.default_funding_rate
        # ближайшая прошедшая ставка
        best, best_ts = self.cost.default_funding_rate, -1
        for fts, rate in self.funding.items():
            if fts <= ts and fts > best_ts:
                best, best_ts = rate, fts
        return best

    def _maybe_funding(self, ts: int, mark: float) -> None:
        if self.last_funding_ts == 0:
            self.last_funding_ts = ts
            return
        if ts - self.last_funding_ts < self.cost.funding_interval_ms:
            return
        self.last_funding_ts = ts
        if self.pos.qty == 0:
            return
        rate = self._funding_rate(ts)
        pay = self.pos.qty * mark * rate * self.mult   # лонг платит при rate>0 (₽ для фьючерса)
        self.cash -= pay
        self.pos.funding_paid += pay
        self.events.append(StepEvent(ts, "Funding", mark, 0, abs(pay), 0,
                                      f"8h funding {rate*100:.3f}%"))

    # ───────── маржа / ликвидация ─────────
    def _check_liquidation(self, ts: int, mark: float) -> bool:
        """Маржинальная ликвидация — событие ОДНОРАЗОВОЕ и терминальное.

        До правки флаг `liquidated` ставился, но торговля продолжалась: `_on_price_event`
        переустанавливал сетку, не сверяясь с флагом, счёт ликвидировался снова и снова
        (на AVAXUSDT — 410 раз за 1000 баров) и уходил в минус на 2.4 своей аллокации.
        При изолированной марже такого не бывает: потеря ограничена внесённым обеспечением,
        остаток забирает страховой фонд биржи."""
        if self.liquidated:
            return True                                       # счёт уничтожен — торговли больше нет
        if self.pos.qty == 0:
            return False
        # Bybit считает ликвидацию по MARK PRICE (сглаженной по индексу), а не по цене
        # последней сделки: last дёргается на тонком стакане и дал бы ложные срабатывания
        # там, где реальная биржа позицию не тронула бы. В бэктесте mark недоступен —
        # тогда честно используем цену бара.
        mark = self.mark_price if self.mark_price > 0 else mark
        notional = abs(self.pos.qty) * mark * self.mult       # деньги (₽ для фьючерса)
        equity = self.cash + self.unrealized_money(mark)
        if equity <= notional * self.cost.maint_margin:
            qty = abs(self.pos.qty)
            side = "sell" if self.pos.qty > 0 else "buy"
            fp = self.cost.fill_price(mark, side, is_maker=False)
            r = self._apply_fill(side, qty, fp, is_maker=False, ts=ts, note="liquidation")
            self.liquidated = True
            self.active = False                               # ← котирование прекращается навсегда
            if self.cash < 0.0:
                self.cash = 0.0                               # изолированная маржа: минус не переносится
            self.events.append(StepEvent(ts, "Ликвидация ⚠", fp, qty,
                                         self.fills[-1].fee, r,
                                         "маржа исчерпана, позиция закрыта, счёт остановлен"))
            self.orders.clear()
            return True
        return False

    def _close_market(self, ts: int, px: float, comment: str) -> None:
        qty = abs(self.pos.qty)
        side = "sell" if self.pos.qty > 0 else "buy"
        fp = self.cost.fill_price(px, side, is_maker=False)
        r = self._apply_fill(side, qty, fp, is_maker=False, ts=ts, note="stop")
        self.events.append(StepEvent(ts, "Stop-Loss", fp, qty, self.fills[-1].fee, r, comment))
        self.orders.clear()

    # ───────── переустановка сетки (ЕДИНЫЙ код Live + бэктест) ─────────
    def _install_grid(self, center: float, ts: int) -> None:
        """Поставить желаемую сетку из Quoter на место сеточных ордеров (ручные не трогаем).
        Один и тот же вызов из step() (бэктест) и on_tick()/process_trade (Live) → одинаковая
        сетка. Заявка дальше ЖДЁТ на уровне — переоценка только после исполнения или при
        существенном сдвиге состояния (см. _state_shifted)."""
        manual = [o for o in self.orders if o.manual]
        if self.p.mode == "manual":
            self.orders = manual
            self._set_anchor(center)
            return
        if self.p.mode == "grid":
            self._install_ladder(center, manual, ts)
            return
        quotes = self.quoter.build(center, self.ind, self.pos, self.p, self.alloc)
        grid = [_OpenOrder(q.side, q.price, q.size, q.level,
                           queue_ahead=self._queue_ahead(q.side, q.price)) for q in quotes]
        self.orders = manual + grid
        self.mm_posted += len(grid)              # для fill-rate (блок F)
        self._set_anchor(center)

    # ───────── биржевые ограничения инструмента ─────────
    def round_price(self, price: float, side: str) -> float:
        """Цена к шагу тика. Округляем КОНСЕРВАТИВНО — buy вниз, sell вверх: заявка
        не должна оказаться ближе к рынку, чем задумано, иначе бумажный счёт получит
        исполнение по цене, до которой рынок в реальности не дошёл."""
        if self.tick_size <= 0 or price <= 0:
            return price
        n = price / self.tick_size
        k = math.floor(n + 1e-9) if side == "buy" else math.ceil(n - 1e-9)
        return round(k * self.tick_size, 12)

    def round_qty(self, qty: float) -> float:
        """Объём вниз к шагу лота. Ниже минимального — биржа отклонит заявку,
        возвращаем 0, и заявка не выставляется вовсе."""
        if qty <= 0:
            return 0.0
        if self.qty_step > 0:
            qty = math.floor(qty / self.qty_step + 1e-9) * self.qty_step
            qty = round(qty, 12)
        if self.min_qty > 0 and qty < self.min_qty - 1e-12:
            return 0.0
        return qty

    # ───────── маржа под ВЫСТАВЛЕННЫЕ заявки ─────────
    def _worst_notional(self, price: float, extra_buy: float = 0.0,
                        extra_sell: float = 0.0) -> float:
        """Худший нотионал позиции, если исполнятся все заявки одной стороны.

        Два принципа, оба взяты у биржи:

        1. Неттинг (one-way, режим Bybit по умолчанию): встречные заявки не могут
           нарастить позицию одновременно, поэтому обеспечение резервируется
           по БОЛЬШЕЙ стороне, а не по сумме обеих. Сетка на $100 капитала выставляет
           $100 покупок и $100 продаж — суммарно $200 нотионала, но риск всё равно $100.
        2. Нотионал заявки считается по ЕЁ СОБСТВЕННОЙ цене, а не по текущей рыночной:
           заявка на покупку по 90 занимает обеспечение под 90, а не под 100.

        extra_* — нотионал ещё не выставленной заявки, для проверки «влезет ли»."""
        pos_notional = self.pos.qty * price * self.mult
        buys = extra_buy + sum(o.size * o.price * self.mult
                               for o in self.orders if o.side == "buy")
        sells = extra_sell + sum(o.size * o.price * self.mult
                                 for o in self.orders if o.side == "sell")
        return max(abs(pos_notional + buys), abs(pos_notional - sells))

    def leverage(self) -> float:
        """Плечо этой сетки: что задал человек, обрезанное пределом биржи.

        Работает как гарантийное обеспечение на срочном рынке: под позицию
        замораживается не весь её размер, а доля 1/leverage. Само по себе плечо
        заявку НЕ увеличивает — оно поднимает потолок, до которого сетке разрешено
        набирать позицию (см. grid_notional_mult)."""
        lev = self.p.leverage if self.p.leverage > 0 else self.cost.leverage
        lev = max(1.0, lev)
        if self.max_leverage > 0:
            lev = min(lev, self.max_leverage)
        return lev

    def liquidation_price(self) -> float:
        """Цена, при которой обеспечения перестанет хватать и позицию закроют.

        Выводится из того же условия, по которому срабатывает _check_liquidation:
        эквити = нотионал × поддерживающая маржа. 0 — позиции нет."""
        q = self.pos.qty
        if abs(q) < 1e-12:
            return 0.0
        mmr = self.cost.maint_margin
        denom = q * ((1.0 - mmr) if q > 0 else (1.0 + mmr))
        if abs(denom) < 1e-12:
            return 0.0
        px = (q * self.pos.avg_entry - self.cash / self.mult) / denom
        return px if px > 0 else 0.0

    def locked_capital(self) -> float:
        """Сколько денег инструмента нельзя забрать обратно в свободные.

        Это обеспечение под открытую позицию и уже стоящие заявки. Забрать
        больше — значит оставить позицию без покрытия, чего биржа бы не позволила.
        Нужно при перераспределении капитала между независимыми сетками."""
        px = self._last_price
        if px <= 0:
            return 0.0
        return self._margin_used(px)

    def _margin_used(self, price: float) -> float:
        """Начальная маржа, занятая позицией и уже выставленными заявками.

        Биржа резервирует обеспечение не только под позицию, но и под лимитки,
        способные её нарастить. Без этого учёта бумажный счёт выставляет заявки,
        которые реальный счёт бы не потянул."""
        return self._worst_notional(price) / self.leverage()

    def _margin_allows(self, side: str, qty: float, price: float) -> bool:
        """Хватает ли свободного обеспечения, чтобы выставить ещё одну заявку."""
        lev = self.leverage()
        mark = price if price > 0 else self._last_price
        equity = self.cash + self.unrealized_money(mark)
        if equity <= 0:
            return False
        add = qty * price * self.mult
        worst = self._worst_notional(mark,
                                     extra_buy=add if side == "buy" else 0.0,
                                     extra_sell=add if side == "sell" else 0.0)
        return worst / lev <= equity + 1e-9

    # ───────── классический грид: лестница и парные заявки ─────────
    def _grid_size_of(self, price: float) -> float:
        """Размер одного ордера лестницы. Привязан к АЛЛОКАЦИИ инструмента:
        предельный нотионал alloc × grid_notional_mult, нарезанный на grid_levels.
        Абсолютная константа order_usd здесь не используется намеренно — именно она
        при корзине из 10 инструментов (alloc ≈ $100, order_usd = $80) давала потолок
        позиции в 8× капитала при заявленном в конфиге плече 3×."""
        if self.p.contract_qty > 0:
            return self.p.contract_qty          # фьючерс: размер в контрактах
        n = max(1, self.p.grid_levels)
        # Плеча здесь намеренно НЕТ. На бирже оно не задаёт размер заявки — только
        # долю, замораживаемую под обеспечение. Размером управляет grid_notional_mult:
        # при mult=1 сетка торгует нотионалом в размер аллокации (фактически 1×),
        # при mult=3 и плече 3× — на все доступные деньги. Потолок ставит _max_contracts.
        usd = self.alloc * self.p.grid_notional_mult / n
        return usd / price if price > 0 else 0.0

    def _install_ladder(self, center: float, manual: list[_OpenOrder], ts: int) -> None:
        """Поставить неподвижную лестницу. Вызывается ОДИН раз при запуске (и при
        ре-анкоре после пробоя) — а не после каждого исполнения, как перецентровка
        в MM-режимах."""
        step = self.quoter.step_size(center, self.ind, self.p)
        quotes = self.ladder.install(center, step, self._grid_size_of)
        if not quotes:
            return
        self.orders = manual
        placed = 0
        for q in quotes:
            if self._add_grid_order(q.side, q.price, q.size, q.level):
                placed += 1
        self.mm_posted += placed
        self._set_anchor(center)
        if placed == 0:
            # Ни одна заявка не прошла биржевые ограничения — чаще всего объём ордера
            # ниже минимального лота инструмента. Молча повторять попытку на каждом
            # тике нельзя: движок уходит в холостой цикл, а пользователь видит пустую
            # сетку без объяснения. Останавливаем инструмент и говорим почему.
            self.active = False
            self.blocked_reason = (
                f"объём ордера ниже минимального лота ({self.min_qty})"
                if self.rejected_min_qty else "не хватает обеспечения под заявки")
            self.events.append(StepEvent(
                ts, "Инструмент отключён", center, 0, 0, 0,
                f"сетка не выставлена: {self.blocked_reason}. "
                f"Увеличьте капитал, уменьшите число уровней или исключите инструмент."))

    def _add_grid_order(self, side: str, price: float, size: float, level: int) -> bool:
        """Выставить сеточную заявку с проверками, которые сделала бы биржа:
        цена по тику, объём по шагу лота и не ниже минимального, обеспечение в наличии.
        Возвращает False, если заявка не прошла — она просто не выставляется."""
        px = self.round_price(price, side)
        qty = self.round_qty(size)
        if px <= 0:
            return False
        if qty <= 0:
            self.rejected_min_qty += 1
            return False
        if not self._margin_allows(side, qty, px):
            self.rejected_margin += 1
            return False
        self.orders.append(_OpenOrder(side, px, qty, level,
                                      queue_ahead=self._queue_ahead(side, px)))
        return True

    def _place_pair(self, side: str, price: float, qty: float, ts: int) -> None:
        """Выставить парную заявку после исполнения: купили на уровне — продаём на
        уровень выше, и наоборот. Это и есть съём прибыли в гриде; перецентровки нет."""
        if not self.ladder.installed:
            # Страховка. Состояние лестницы могло не восстановиться — например, сессия
            # была сохранена версией, которая его ещё не писала, а ордера при этом
            # восстановились. Тогда pair() вернул бы None, парный take-profit не встал бы,
            # и сетка молча выродилась бы в односторонний набор входов: позиция растёт
            # до потолка, продажи нет. Шаг в этом случае берём из параметров.
            step = self.quoter.step_size(price, self.ind, self.p)
            if step > 0:
                self.ladder.center = price
                self.ladder.step = step
                self.ladder.installed = True
        q = self.ladder.pair(side, price, qty)
        if q is None or q.price <= 0 or q.size <= 0:
            return
        px = self.round_price(q.price, q.side)
        if any(not o.manual and o.side == q.side and abs(o.price - px) < 1e-9
               for o in self.orders):
            return                                  # на этом уровне заявка уже стоит
        if self._add_grid_order(q.side, q.price, q.size, q.level):
            self.mm_posted += 1

    def _ref_mid(self, fallback: float) -> float | None:
        """Справедливая середина живой книги. None — если книги нет.

        Возвращать вместо неё цену тика нельзя: та всегда по «нашу» сторону уровня,
        и разложение PnL на спред и инвентарь получается ложным (спред-доход выходит
        структурно отрицательным). Лучше честно не считать, чем считать неверно."""
        b = self.ob_book
        if b:
            bids = b.get("b") or []
            asks = b.get("a") or []
            if bids and asks:
                bb, ba = bids[0][0], asks[0][0]
                if bb > 0 and ba > 0:
                    return (bb + ba) / 2.0
        return None

    def _resolve_markout(self, ts: int, price: float) -> None:
        """Разрешить созревшие markout-замеры: цена через mk_horizon_ms после филла,
        знак в пользу мейкера (buy: цена выросла = +; sell: упала = +). <0 = adverse selection."""
        pend = self._mk_pending
        while pend and ts - pend[0][0] >= self.mk_horizon_ms:
            f_ts, f_px, f_side = pend.popleft()
            if f_px > 0:
                mo = (price - f_px) if f_side == "buy" else (f_px - price)
                self.mk_sum += mo / f_px
                self.mk_n += 1
                if mo < 0:
                    self.mk_adverse += 1

    def _queue_ahead(self, side: str, level: float) -> float:
        """ОЦЕНКА объёма перед нами на уровне из живой книги (live, выбранный символ). В
        бэктесте/replay реальной книги нет → 0. Документированная граница (блок C):
        публичный L2 даёт агрегатный объём, не истинную очередь."""
        if not self.ob_book:
            return 0.0
        from .queue_fill import book_queue_ahead
        levels = self.ob_book.get("b" if side == "buy" else "a") or []
        return book_queue_ahead(levels, level, side)

    def _set_anchor(self, center: float) -> None:
        inv = (self.pos.qty * center) / self.alloc if self.alloc > 0 else 0.0
        self._quote_anchor = (center, self.ind.sigma, inv)

    def _state_shifted(self, price: float) -> bool:
        """Существенно ли изменилось состояние с момента котирования → переоценка котировок.
        ОДИНАКОВО в Live и бэктесте (общий путь). Иначе заявка ЖДЁТ на уровне."""
        am, asig, ainv = self._quote_anchor
        if am <= 0:
            return True
        step = self.quoter.last_step or (self.ind.atr or am * 0.005)
        if abs(price - am) >= self.requote_frac * max(step, 1e-9):
            return True
        inv = (self.pos.qty * price) / self.alloc if self.alloc > 0 else 0.0
        if abs(inv - ainv) >= self.requote_inv:
            return True
        if asig > 0 and abs(self.ind.sigma - asig) / asig >= self.requote_sigma:
            return True
        return False

    def grid_info(self) -> dict:
        """Реальные шаг/диапазон сетки движка — для панели «ТЕКУЩАЯ СЕТКА»."""
        prices = [o.price for o in self.orders if not o.manual]
        if self.p.mode == "grid" and self.ladder.installed:
            return {"step": round(self.ladder.step, 4), "center": round(self.ladder.center, 4),
                    "low": round(min(prices), 4) if prices else None,
                    "high": round(max(prices), 4) if prices else None,
                    "count": len(prices)}
        return {"step": round(self.quoter.last_step, 4) if self.quoter.last_step else None,
                "low": round(min(prices), 4) if prices else None,
                "high": round(max(prices), 4) if prices else None,
                "count": len(prices)}

    # ───────── пауза сессии (закрытие/открытие дня MOEX) ─────────
    def _in_session_pause(self, ts: int) -> bool:
        """True если бар попадает в зону паузы (N минут до конца / после начала дня)."""
        pause = self.p.session_pause
        if pause <= 0:
            return False
        dt = datetime.datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=_MSK)
        t = dt.time()
        # до конца дня: [23:50 - pause .. 23:59]
        end_boundary = (datetime.datetime.combine(dt.date(), _SESSION_END, tzinfo=_MSK)
                        - datetime.timedelta(minutes=pause)).time()
        if t >= end_boundary:
            return True
        # после начала дня: [10:00 .. 10:00 + pause]
        start_boundary = (datetime.datetime.combine(dt.date(), _SESSION_START, tzinfo=_MSK)
                          + datetime.timedelta(minutes=pause)).time()
        if t <= start_boundary and t >= _SESSION_START:
            return True
        return False

    def _flatten_for_pause(self, price: float, ts: int) -> None:
        """Снять сеточные ордера и закрыть позицию по рынку. Ручные ордера остаются."""
        self.orders = [o for o in self.orders if o.manual]
        if self.pos.qty != 0:
            side = "sell" if self.pos.qty > 0 else "buy"
            qty = abs(self.pos.qty)
            r = self._apply_fill(side, qty, price, is_maker=False, ts=ts, note="session close")
            self.events.append(StepEvent(ts, "Пауза сессии", price, qty,
                                        self.fills[-1].fee, r, "закрытие позиции, пауза сессии"))

    # ───────── единый каденс: общий путь Live и бэктеста ─────────
    @staticmethod
    def _bar_ticks(bar: Candle) -> list[float]:
        """Внутрибарный путь цены как последовательность тиков. Бэктест раскладывает бар
        на эти тики и гонит через ТОТ ЖЕ _on_price_event, что и Live → структурный паритет."""
        if bar.c >= bar.o:                       # рост: o → l → h → c
            return [bar.o, bar.l, bar.h, bar.c]
        return [bar.o, bar.h, bar.l, bar.c]      # падение: o → h → l → c

    def _handle_pause(self, ts: int, price: float) -> bool:
        if not self._in_session_pause(ts):
            return False
        has_grid = any(not o.manual for o in self.orders)
        if self.pos.qty != 0 or has_grid:
            self._flatten_for_pause(price, ts)
        return True

    def _on_price_event(self, price: float, ts: int) -> None:
        """Единый матчинг по одному тику цены (бэктест-тик и Live-тик). Заявка ЖДЁТ на
        уровне; исполняется только когда цена реально прошла ЧЕРЕЗ уровень (trade-through
        по цене); после исполнения или существенного сдвига состояния — переоценка по A-S."""
        if not self.ind._seeded or price <= 0:
            return
        self._maybe_funding(ts, price)
        if not self.ind.ready:
            self._last_price = price
            return
        self._resolve_markout(ts, price)
        if self._handle_pause(ts, price):
            self._last_price = price
            return
        if self.active and not self.liquidated and not self.halted \
                and not any(not o.manual for o in self.orders):
            self._install_grid(price, ts)
        grid_filled = self._match_tick(price, ts)
        self._requote_and_risk(price, ts, grid_filled)
        self._last_price = price

    def _match_tick(self, price: float, ts: int) -> bool:
        """Тиковый trade-through: лимитка исполняется, когда цена прошла через её уровень.
        Заявка НЕ перецентруется — ждёт. Возвращает True, если исполнилась сеточная заявка."""
        grid_filled = False
        for o in list(self.orders):
            crossed = (o.side == "buy" and price <= o.price) or (o.side == "sell" and price >= o.price)
            if not crossed:
                continue
            if o.manual:
                self._consume(o, o.price, float("inf"), ts, manual=True, mid=None)
            elif self.active:
                # mid=None: в тиковом пути книги нет, справедливую середину взять неоткуда
                if self._consume(o, o.price, float("inf"), ts, manual=False, mid=None) > 0 \
                        and o.size <= 1e-12:
                    grid_filled = True   # перецентровка ТОЛЬКО при ПОЛНОМ филле (паритет с Live)
        return grid_filled

    def _consume(self, o: _OpenOrder, fill_price: float, avail: float, ts: int,
                 manual: bool, mid: float | None = None,
                 trade_price: float | None = None) -> float:
        """Исполнить заявку o на доступный объём avail после ТАЯНИЯ очереди. avail=inf —
        тиковый путь (цена прошла уровень → полный филл); реальный объём сделки — для
        честного trade-through по потоку publicTrade (блок C). Частичное — по объёму.
        mid — справедливая середина для разложения PnL (блок F).
        trade_price — цена РЕАЛЬНОЙ сделки (process_trade); по ней детектируется swept.
        По умолчанию = fill_price (тиковый путь, где реальной ленты нет)."""
        # Swept: реальная сделка прошла СТРОГО за наш уровень → весь реальный объём перед
        # нами исчерпан (исполнения + отмены); иначе очередь тает лишь по объёму сделок, а
        # отмены видны только в L2, не в ленте. Мейкер-филл идёт по fill_price (==o.price у
        # callsite), поэтому swept сравниваем по ЦЕНЕ СДЕЛКИ, а не по цене ордера.
        tp = fill_price if trade_price is None else trade_price
        if o.queue_ahead > 0 and (
            (o.side == "buy" and tp < o.price - 1e-9) or
            (o.side == "sell" and tp > o.price + 1e-9)
        ):
            o.queue_ahead = 0.0
        eaten = min(o.queue_ahead, avail)
        o.queue_ahead -= eaten
        avail -= eaten
        if avail <= 1e-12:
            return 0.0                            # сделка ушла на очередь перед нами — не исполнились
        qty = min(o.size, avail)
        if not manual:
            qty = self._allowed_qty(o.side, qty, fill_price)   # жёсткий лимит инвентаря (блок G)
        if qty <= 1e-12:
            return 0.0
        self._uid += 1
        r = self._apply_fill(o.side, qty, fill_price, is_maker=True, ts=ts,
                             note=("ручной" if manual else f"L{o.level} fill"), mid=mid)
        if not manual:
            self.mm_filled += 1                                 # fill-rate
            self._mk_pending.append((ts, fill_price, o.side))   # markout (adverse selection)
            if self.p.mode == "grid":
                self._place_pair(o.side, fill_price, qty, ts)   # парный take-profit
        o.size -= qty
        partial = o.size > 1e-12
        if not partial:
            try:
                self.orders.remove(o)
            except ValueError:
                pass
        if manual:
            act = "Лимит Buy" if o.side == "buy" else "Лимит Sell"
            self.events.append(StepEvent(ts, act, fill_price, qty, self.fills[-1].fee, r,
                                         "ручной ордер исполнен"))
        else:
            act = "Grid Buy" if o.side == "buy" else "Grid Sell"
            comment = "вход в позицию" if r == 0 else "закрытие round-trip"
            if partial:
                comment += " (partial)"
            self.events.append(StepEvent(ts, act, fill_price, qty, self.fills[-1].fee, r, comment))
        return qty

    def _requote_and_risk(self, price: float, ts: int, grid_filled: bool) -> None:
        """После исполнения или существенного сдвига — переставить сетку по A-S; затем риск."""
        if self._check_killswitch(ts, price):            # kill-switch по просадке (блок G)
            return
        if self.active and not self._in_session_pause(ts):
            if self.p.mode == "grid":
                # Классический грид НЕ перецентруется: уровни неподвижны, прибыль снимают
                # парные заявки из _place_pair. Переустановка — только при явном пробое
                # диапазона лестницы (grid_reanchor > 0), по умолчанию никогда.
                if self.ladder.out_of_range(price, self.p.grid_reanchor):
                    self._install_grid(price, ts)
            elif grid_filled:
                self._install_grid(price, ts)   # перецентровка ТОЛЬКО после исполнения (не по _state_shifted)
        # стоп-лосса НЕТ: выход из позиции — встречной лимиткой (sell сверху), а не тейкером по рынку.
        # buy снизу + sell сверху сами балансируют позицию. _check_stop_tick намеренно не вызывается.
        self._check_liquidation(ts, price)   # только маржинальная ликвидация (биржевая, не стоп стратегии)
        if self.active and not self.liquidated and not self._in_session_pause(ts) \
                and not any(not o.manual for o in self.orders):
            self._install_grid(price, ts)

    # ───────── один бар (бэктест) ─────────
    def step(self, bar: Candle) -> None:
        self.ind.update(bar)                      # индикаторы — РАЗ на бар (как в Live на закрытии)
        if not self.ind.ready:
            self._last_price = bar.c
            self.equity_curve.append(self.cash + self.pos.unrealized(bar.c))
            return
        if not self.active and self.p.mode != "manual" \
                and not self.blocked_reason and not self.liquidated:
            # бэктест/replay через step = стратегия работает. Но заблокированный
            # биржевыми ограничениями или ликвидированный инструмент не воскрешаем:
            # иначе движок будет заново пытаться выставить сетку на каждом баре.
            self.active = True
        for px in self._bar_ticks(bar):           # тот же путь, что у Live (on_tick)
            self._on_price_event(px, bar.ts)
        self._last_price = bar.c
        self.equity_curve.append(self.cash + self.pos.unrealized(bar.c))
        self.inv_curve.append(self.pos.qty)       # инвентарь во времени (блок F)

    # ───────── батч ─────────
    def run(self, candles: list[Candle]) -> None:
        for c in candles:
            self.step(c)

    # ═════════ REALTIME (тиковый grid) ═════════
    def _order_qty(self, price: float) -> float:
        return self.p.order_usd / price if price > 0 else 0.0

    def warm(self, candles: list[Candle]) -> None:
        """Прогреть индикаторы на истории БЕЗ торговли (ATR/EMA для шага сетки)."""
        for c in candles:
            self.ind.update(c)
        if candles:
            self._last_price = candles[-1].c

    def _grid_step(self, price: float) -> float:
        return self.quoter.step_size(price, self.ind, self.p)

    def start_strategy(self, price: float) -> None:
        """Запуск: выставить многоуровневую сетку через единый Quoter (модель реально работает)."""
        self.active = True
        self.halted = False                                    # снять kill-switch при ручном перезапуске
        self.peak_equity = self.cash + self.unrealized_money(price)  # просадка считается от рестарта
        self._install_grid(price, 0)
        n = sum(1 for o in self.orders if not o.manual)
        self.events.append(StepEvent(0, "Старт стратегии", price, 0, 0, 0,
                                     f"сетка выставлена: {n} уровней ({self.p.mode})"))

    def stop_strategy(self) -> None:
        """Остановить стратегию: снять все сеточные ордера (ручные остаются)."""
        self.active = False
        self.orders = [o for o in self.orders if o.manual]
        self.events.append(StepEvent(0, "Стоп стратегии", self._last_price, 0, 0, 0,
                                     "сеточные ордера сняты"))

    def add_manual_order(self, side: str, price: float, qty: float) -> int:
        """Поставить ручную лимитку (мышью на графике), как на бирже."""
        self._uid += 1
        self.orders.append(_OpenOrder(side, price, qty, 0, manual=True, oid=self._uid))
        return self._uid

    def cancel_manual(self, oid: int) -> None:
        self.orders = [o for o in self.orders if not (o.manual and o.oid == oid)]

    def on_tick(self, price: float, ts: int) -> None:
        """Один рыночный тик (Live по тикеру / replay). В обычном режиме — единый матчинг
        _on_price_event (заявка ждёт уровень; переоценка после филла или сдвига состояния).
        В режиме trade_driven фактические филлы приходят из РЕАЛЬНЫХ сделок (process_trade),
        а тик лишь маркирует цену, начисляет funding/риск и переоценку по порогу."""
        if not self.trade_driven:
            self._on_price_event(price, ts)
            self._sample_inv()
            return
        if not self.ind._seeded or price <= 0:
            return
        self._maybe_funding(ts, price)
        self._resolve_markout(ts, price)
        if self._handle_pause(ts, price):
            self._last_price = price
            return
        if self.active and not self.liquidated and not self.halted \
                and not any(not o.manual for o in self.orders):
            self._install_grid(price, ts)
        self._requote_and_risk(price, ts, False)
        self._last_price = price
        self._sample_inv()

    def process_trade(self, price: float, size: float, side: str, ts: int) -> None:
        """ЧЕСТНЫЙ trade-through по РЕАЛЬНОЙ сделке из потока publicTrade (блок C): buy-лимитка
        исполняется встречной продажей (side='Sell') по цене<=уровня; sell — покупкой
        (side='Buy') по цене>=уровня; по реально прошедшему ОБЪЁМУ, с таянием очереди. После
        исполнения — переоценка по A-S. В Live для выбранного символа (поток из блока B)."""
        if not self.ind._seeded or price <= 0 or size <= 0:
            return
        self._maybe_funding(ts, price)
        self._resolve_markout(ts, price)
        if self._handle_pause(ts, price):
            self._last_price = price
            return
        if self.active and not self.liquidated and not self.halted \
                and not any(not o.manual for o in self.orders):
            self._install_grid(price, ts)
        mid = self._ref_mid(price)                 # справедливая середина книги для разложения PnL
        grid_filled = False
        for o in list(self.orders):
            if o.side == "buy":
                through = side == "Sell" and price <= o.price + 1e-9
            else:
                through = side == "Buy" and price >= o.price - 1e-9
            if not through:
                continue
            if o.manual:
                self._consume(o, o.price, size, ts, manual=True, mid=mid, trade_price=price)
            elif self.active:
                if self._consume(o, o.price, size, ts, manual=False, mid=mid,
                                 trade_price=price) > 0 and o.size <= 1e-12:
                    grid_filled = True   # перецентровка ТОЛЬКО при ПОЛНОМ филле (ордер снят)
        self._requote_and_risk(price, ts, grid_filled)
        self._last_price = price
        self._sample_inv()

    def _check_stop_tick(self, price: float, ts: int) -> None:
        if self.p.sl <= 0 or self.pos.qty == 0 or self.ind.atr <= 0:
            return
        sl = self.p.sl * self.ind.atr
        if self.pos.qty > 0 and price <= self.pos.avg_entry - sl:
            self._close_market(ts, self.pos.avg_entry - sl, "SL hit, позиция закрыта")
        elif self.pos.qty < 0 and price >= self.pos.avg_entry + sl:
            self._close_market(ts, self.pos.avg_entry + sl, "SL hit, позиция закрыта")

    # ───────── риск-контроль (блок G) ─────────
    def _max_contracts(self, price: float) -> float:
        """ЖЁСТКИЙ потолок позиции в контрактах. Два независимых ограничения, берётся
        строгое из них:

        1) число единиц инвентаря — grid_levels в режиме 'grid', иначе max_orders;
        2) ПЛЕЧО: нотионал не выше alloc × leverage — то, что заявлено в .env.

        До правки действовало только (1), и плечо из конфига не проверялось нигде.
        На корзине из 10 инструментов (alloc ≈ $100, order_usd = $80, max_orders = 10)
        потолок позиции выходил $800 — восьмикратное плечо при LEVERAGE=3.0."""
        if price <= 0:
            return 0.0
        caps: list[float] = []
        if self.p.mode == "grid":
            n = max(1, self.p.grid_levels)
            caps.append(n * self.p.contract_qty if self.p.contract_qty > 0
                        else self.alloc * self.p.grid_notional_mult / price)
        elif self.p.max_orders > 0:
            caps.append(self.p.max_orders * self.p.contract_qty if self.p.contract_qty > 0
                        else self.p.max_orders * (self.p.order_usd / price))
        if self.alloc > 0:
            caps.append(self.alloc * self.leverage() / price)
        return min(caps) if caps else 0.0

    def _allowed_qty(self, side: str, qty: float, price: float) -> float:
        """Урезать объём филла так, чтобы НЕ превысить жёсткий лимит позиции на стороне
        набора. Сокращение/разворот в пределах лимита — без ограничений."""
        cap = self._max_contracts(price)
        if cap <= 0:
            return qty
        signed = qty if side == "buy" else -qty
        new = self.pos.qty + signed
        if abs(new) <= cap + 1e-12 or abs(new) <= abs(self.pos.qty):
            return qty                                   # в пределах лимита или снижаем экспозицию
        allowed_signed = (cap if signed > 0 else -cap) - self.pos.qty
        if (allowed_signed > 0) != (signed > 0):
            return 0.0                                   # уже за лимитом на этой стороне — добор запрещён
        return max(0.0, min(qty, abs(allowed_signed)))   # добор только до лимита

    def _check_killswitch(self, ts: int, price: float) -> bool:
        """Kill-switch по просадке: при просадке счёта >= max_drawdown_pct закрыть позицию
        по рынку и остановить котирование. Возвращает True, если движок остановлен."""
        eq = self.cash + self.unrealized_money(price)
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.halted:
            return True
        thr = self.p.max_drawdown_pct
        if thr <= 0 or self.peak_equity <= 0:
            return False
        dd = (self.peak_equity - eq) / self.peak_equity * 100.0
        if dd >= thr:
            if self.pos.qty != 0:
                self._close_market(ts, price, f"kill-switch: просадка {dd:.1f}%")
            self.orders = [o for o in self.orders if o.manual]
            self.halted = True
            self.active = False
            self.events.append(StepEvent(ts, "Kill-switch", price, 0, 0, 0,
                                         f"просадка {dd:.1f}% >= порога {thr:.1f}% — котирование остановлено"))
            return True
        return False

    # ───────── метрики маркет-мейкинга (блок F) ─────────
    def _sample_inv(self, cap: int = 600) -> None:
        self.inv_curve.append(self.pos.qty)
        if cap and len(self.inv_curve) > cap:
            self.inv_curve.pop(0)

    def reset_mm(self) -> None:
        """Обнулить аккумуляторы MM-метрик (новый счёт)."""
        self.mm_spread_px = 0.0
        self.mm_spread_n = 0
        self.mm_posted = 0
        self.mm_filled = 0
        self.inv_curve = []
        self.mk_sum = 0.0
        self.mk_n = 0
        self.mk_adverse = 0
        self._mk_pending.clear()

    def mm_metrics(self) -> dict:
        """Разложение PnL и метрики MM. spread_pnl + inventory_pnl − fees СХОДИТСЯ с общим
        PnL (equity − alloc) — контроль. Только факты, владелец трактует.
          spread_pnl    — доход от спреда (мейкер купил ниже / продал выше mid);
          inventory_pnl — PnL от движения цены против накопленной позиции;
          fill_rate     — исполнено/выставлено; avg_markout_bps<0 — adverse selection."""
        M = self._last_price
        total_price = self.pos.realized + self.unrealized_money(M)   # фактический ценовой PnL движка
        # Разложение на спред и инвентарь имеет смысл ТОЛЬКО если известна середина
        # книги на момент филла. В бэктесте L2 нет — тогда отдаём None, а не число:
        # ложная разбивка хуже отсутствующей. Сумма (total_price) верна всегда.
        has_mid = self.mm_spread_n > 0
        spread = (self.mm_spread_px * self.mult) if has_mid else None
        inv = (total_price - spread) if has_mid else None
        fees = self.pos.fees_paid
        funding = self.pos.funding_paid
        markout_bps = (self.mk_sum / self.mk_n * 1e4) if self.mk_n else None
        adverse_rate = (self.mk_adverse / self.mk_n) if self.mk_n else None
        return {
            "spread_pnl": round(spread, 4) if has_mid else None,
            "inventory_pnl": round(inv, 4) if has_mid else None,
            "decomposition_available": has_mid,   # False → нет L2-книги, разбивка не считается
            "fees": round(fees, 4),
            "funding": round(funding, 4),
            "net_pnl": round(total_price - fees - funding, 4),   # == equity − alloc (контроль)
            "total_price_pnl": round(total_price, 4),
            "posted": self.mm_posted,
            "filled": self.mm_filled,
            "fill_rate": round(self.mm_filled / self.mm_posted, 4) if self.mm_posted else 0.0,
            "avg_spread_per_fill": (round(spread / self.mm_filled, 6)
                                    if has_mid and self.mm_filled else None),
            "avg_markout_bps": round(markout_bps, 3) if markout_bps is not None else None,
            "adverse_rate": round(adverse_rate, 4) if adverse_rate is not None else None,
            "markout_n": self.mk_n,
            "inventory": round(self.pos.qty, 8),
            "max_position": round(self._max_contracts(M), 8),     # жёсткий лимит позиции (контракты)
            "halted": self.halted,                                # сработал ли kill-switch
            "drawdown_pct": round((self.peak_equity - (self.cash + self.unrealized_money(M)))
                                  / self.peak_equity * 100.0, 3) if self.peak_equity > 0 else 0.0,
            "trades": self.trades,
        }

    # ───────── сохранение/восстановление состояния (как биржевой аккаунт) ─────────
    def to_state(self) -> dict:
        return {
            "cash": self.cash,
            # Доля капитала, выданная этой сетке. Она больше не выводится из
            # risk-parity по всей корзине — сетка получает деньги при запуске
            # и возвращает при остановке, поэтому её надо хранить.
            "alloc": self.alloc,
            "pos": {"qty": self.pos.qty, "avg_entry": self.pos.avg_entry,
                    "realized": self.pos.realized, "fees_paid": self.pos.fees_paid,
                    "funding_paid": self.pos.funding_paid},
            "last_funding_ts": self.last_funding_ts,
            "liquidated": self.liquidated,
            "trades": self.trades,
            "buy_filled": self.buy_filled,
            "sell_filled": self.sell_filled,
            "active": self.active,
            "uid": self._uid,
            "orders": [{"side": o.side, "price": o.price, "size": o.size, "level": o.level,
                        "manual": o.manual, "oid": o.oid} for o in self.orders],
            # Центр и шаг лестницы обязаны пережить рестарт вместе с ордерами: без них
            # GridLadder поднимается неустановленной, pair() возвращает None, и филл
            # сеточной заявки перестаёт порождать парный take-profit — сетка молча
            # превращается в набор односторонних входов.
            "ladder": {"center": self.ladder.center, "step": self.ladder.step,
                       "installed": self.ladder.installed,
                       "n": self.ladder.n, "side": self.ladder.side},
            # Причина блокировки обязана пережить рестарт вместе с лестницей.
            # Иначе инструмент поднимается с installed=True и нулём ордеров:
            # _install_ladder больше не вызывается, причина не восстанавливается,
            # и в интерфейсе он выглядит «активен · 0 орд» без единого объяснения,
            # почему сетки нет.
            "roundtrips": self.roundtrips[-200:],
            "blocked_reason": self.blocked_reason,
            "rejected_min_qty": self.rejected_min_qty,
            "rejected_margin": self.rejected_margin,
        }

    def load_state(self, st: dict) -> None:
        self.cash = st["cash"]
        p = st.get("pos", {})
        self.pos.qty = p.get("qty", 0.0)
        self.pos.avg_entry = p.get("avg_entry", 0.0)
        self.pos.realized = p.get("realized", 0.0)
        self.pos.fees_paid = p.get("fees_paid", 0.0)
        self.pos.funding_paid = p.get("funding_paid", 0.0)
        self.last_funding_ts = st.get("last_funding_ts", 0)
        self.liquidated = st.get("liquidated", False)
        self.trades = st.get("trades", 0)
        self.buy_filled = st.get("buy_filled", 0.0)
        self.sell_filled = st.get("sell_filled", 0.0)
        self.active = st.get("active", False)
        self._uid = st.get("uid", 0)
        self.orders = [_OpenOrder(o["side"], o["price"], o["size"], o.get("level", 0),
                                  o.get("manual", False), o.get("oid", 0))
                       for o in st.get("orders", [])]
        lad = st.get("ladder") or {}
        if lad.get("installed") and lad.get("step", 0) > 0:
            self.ladder.center = float(lad["center"])
            self.ladder.step = float(lad["step"])
            self.ladder.n = int(lad.get("n", self.ladder.n))
            self.ladder.side = lad.get("side", self.ladder.side)
            self.ladder.installed = True
            # Лестница, восстановленная без единой живой заявки, установленной
            # не является. Помечаем её как неустановленную, чтобы движок попробовал
            # выставить сетку заново — и либо выставил, либо честно записал причину,
            # почему не может. Иначе инструмент навсегда зависает в состоянии
            # «установлено, ордеров ноль, объяснений нет».
            if not self.orders:
                self.ladder.installed = False
        self.roundtrips = list(st.get("roundtrips") or [])
        self.blocked_reason = st.get("blocked_reason", "")
        self.rejected_min_qty = st.get("rejected_min_qty", 0)
        self.rejected_margin = st.get("rejected_margin", 0)
        # Восстановление из состояния, записанного версией без blocked_reason:
        # инструмент выключен, ордеров нет, ликвидации не было, причины тоже нет.
        # Такой останавливается навсегда и молча. Даём ему ровно одну попытку
        # выставиться заново — она либо сработает, либо запишет причину.
        if (not self.active and not self.liquidated and not self.blocked_reason
                and not self.orders):
            self.active = True
            self.ladder.installed = False

    # ───────── сводка ─────────
    def unrealized_money(self, mark: float) -> float:
        """Нереализованный PnL в деньгах (₽ для фьючерса через mult)."""
        return self.pos.qty * (mark - self.pos.avg_entry) * self.mult

    def equity(self) -> float:
        return self.cash + self.unrealized_money(self._last_price)

    def summary(self) -> dict:
        eq = self.equity()
        return {
            "sym": self.sym,
            "alloc": self.alloc,
            "equity": eq,
            "pnl": eq - self.alloc,
            "pnl_pct": (eq - self.alloc) / self.alloc * 100.0 if self.alloc else 0.0,
            "realized": self.pos.realized,
            "fees": self.pos.fees_paid,
            "funding": self.pos.funding_paid,
            "trades": self.trades,
            "orders_open": len(self.orders),
            "liquidated": self.liquidated,
            "qty": self.pos.qty,
            "avg_entry": self.pos.avg_entry,
            # Почему инструмент не торгуется, если не торгуется: биржевые ограничения
            # (минимальный лот, обеспечение) должны быть видны, а не прятаться в нулях.
            "blocked": self.blocked_reason,
            "min_order_usd": round(self.min_qty * self._last_price, 2) if self.min_qty else 0.0,
            "rejected_min_qty": self.rejected_min_qty,
            "rejected_margin": self.rejected_margin,
        }
