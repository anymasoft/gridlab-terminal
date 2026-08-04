"""Честный эмулятор исполнения лимиток по РЕАЛЬНОМУ trade-through (блок C).
Перенос идеи из Polymarket (_work_resting / _post_maker, pm01.py): лимитка
исполняется ТОЛЬКО когда через её уровень реально прошла сделка из потока
publicTrade (блок B) — не на дрейфе котировок и не «по касанию» H/L.

ПРАВИЛО ИСПОЛНЕНИЯ
------------------
buy-лимитка на уровне L исполняется встречной СДЕЛКОЙ тейкера-продавца (S='Sell')
по цене <= L; sell-лимитка на L — сделкой тейкера-покупателя (S='Buy') по цене >= L.
По частичному — на реально прошедший через уровень ОБЪЁМ.

ОЦЕНКА ОЧЕРЕДИ (документированная ГРАНИЦА ЧЕСТНОСТИ)
---------------------------------------------------
При постановке заявки объём, уже стоявший на уровне, = объём ПЕРЕД нами (мы в конце
очереди). По мере сделок через уровень этот объём ТАЕТ; исполняемся, когда перед нами
ноль и сделка реально прошла. ЭТО ОЦЕНКА: публичный L2 даёт АГРЕГАТНЫЙ объём на
уровне, не истинную очередь и не порядок заявок (как dip_cents на Polymarket — не
претензия на точность, а честная, помеченная граница).

Наивная модель NaiveTouchFiller (для сравнения) исполняет лимитку на ПЕРВОМ касании
уровня ценой, без очереди и встречного объёма — она ЗАВЫШАЕТ филлы. Сравнение в цифрах
показывает, сколько филлов «дарит» нечестное касание.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from ..marketdata.orderbook import Trade

_EPS = 1e-9


@dataclass
class Fill:
    order_id: int
    side: str
    ts: int
    price: float          # цена нашего уровня (мейкер)
    size: float           # исполненный объём в этом филле
    trade_price: float    # цена реальной сделки, что нас исполнила
    penetration: float    # насколько сделка прошла ЗА уровень (dip-аналог), в цене


@dataclass
class RestingOrder:
    """Лежащая лимитка. queue_ahead — ОЦЕНКА объёма перед нами на уровне (тает)."""
    oid: int
    side: str             # 'buy' | 'sell'
    price: float
    size: float           # полный объём заявки (база)
    queue_ahead: float = 0.0
    posted_ts: int = 0
    mid_at_post: float = 0.0
    # состояние исполнения
    filled: float = 0.0
    vwap: float = 0.0
    n_fills: int = 0
    first_fill_ts: int = 0
    penetration: float = 0.0    # макс. проникновение сделки за уровень (dip-аналог)
    done: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def is_filled(self) -> bool:
        return self.filled >= self.size - 1e-12


class TradeThroughFiller:
    """ЧЕСТНЫЙ эмулятор: trade-through + тающая очередь + частичные по объёму."""

    def __init__(self) -> None:
        self.orders: list[RestingOrder] = []
        self.fills: list[Fill] = []
        self.posted = 0
        self._uid = 0

    def post(self, side: str, price: float, size: float, queue_ahead: float = 0.0,
             ts: int = 0, mid_at_post: float = 0.0) -> RestingOrder:
        self._uid += 1
        o = RestingOrder(oid=self._uid, side=side, price=float(price), size=float(size),
                         queue_ahead=max(0.0, float(queue_ahead)), posted_ts=ts,
                         mid_at_post=mid_at_post)
        self.orders.append(o)
        self.posted += 1
        return o

    def on_trade(self, tr: Trade) -> None:
        """Подаётся каждая реальная сделка (совместимо с OrderBookFeed.on_trade)."""
        for o in self.orders:
            if not o.done:
                self._match(o, tr)

    def _match(self, o: RestingOrder, tr: Trade) -> None:
        # trade-through нашего уровня встречным тейкером
        if o.side == "buy":
            through = tr.side == "Sell" and tr.price <= o.price + _EPS
        else:
            through = tr.side == "Buy" and tr.price >= o.price - _EPS
        if not through:
            return
        vol = tr.size
        # сначала тает очередь перед нами
        eaten = min(o.queue_ahead, vol)
        o.queue_ahead -= eaten
        vol -= eaten
        if vol <= _EPS:
            return                              # сделка ушла на очередь перед нами — мы НЕ исполнились
        fill = min(o.remaining, vol)
        if fill <= _EPS:
            return
        pen = (o.price - tr.price) if o.side == "buy" else (tr.price - o.price)
        o.penetration = max(o.penetration, pen)
        prev = o.vwap * o.filled
        o.filled += fill
        o.vwap = (prev + o.price * fill) / o.filled
        o.n_fills += 1
        if not o.first_fill_ts:
            o.first_fill_ts = tr.ts
        self.fills.append(Fill(order_id=o.oid, side=o.side, ts=tr.ts, price=o.price,
                               size=fill, trade_price=tr.price, penetration=pen))
        if o.is_filled:
            o.done = True


class NaiveTouchFiller:
    """НАИВНАЯ модель (как прежний матчинг по H/L бара): филл на ПЕРВОМ касании уровня
    ценой, мгновенно и на весь объём, без очереди и без учёта встречного объёма/стороны.
    Намеренно ЗАВЫШАЕТ филлы — служит верхней границей для сравнения."""

    def __init__(self) -> None:
        self.orders: list[RestingOrder] = []
        self.fills: list[Fill] = []
        self.posted = 0
        self._uid = 0

    def post(self, side: str, price: float, size: float, queue_ahead: float = 0.0,
             ts: int = 0, mid_at_post: float = 0.0) -> RestingOrder:
        self._uid += 1
        o = RestingOrder(oid=self._uid, side=side, price=float(price), size=float(size),
                         queue_ahead=0.0, posted_ts=ts, mid_at_post=mid_at_post)
        self.orders.append(o)
        self.posted += 1
        return o

    def on_trade(self, tr: Trade) -> None:
        for o in self.orders:
            if o.done:
                continue
            touch = (o.side == "buy" and tr.price <= o.price + _EPS) or \
                    (o.side == "sell" and tr.price >= o.price - _EPS)
            if touch:
                o.filled = o.size
                o.vwap = o.price
                o.n_fills = 1
                o.first_fill_ts = tr.ts
                o.done = True
                self.fills.append(Fill(order_id=o.oid, side=o.side, ts=tr.ts, price=o.price,
                                       size=o.size, trade_price=tr.price, penetration=0.0))


# ───────────────────────── метрики (факты, без вердиктов) ─────────────────────────
def _price_at(ts_sorted: list[int], prices: list[float], t: int) -> float:
    """Цена последней сделки с ts <= t (для markout после филла)."""
    if not ts_sorted:
        return 0.0
    i = bisect.bisect_right(ts_sorted, t) - 1
    i = min(max(i, 0), len(prices) - 1)
    return prices[i]


def fill_metrics(filler, trades: list[Trade], horizon_ms: int = 2000) -> dict:
    """Метрики исполнения. markout — изменение цены через horizon_ms ПОСЛЕ филла, со
    знаком в пользу мейкера (>0 благоприятно, <0 = adverse selection). penetration —
    как глубоко сделка прошла за уровень (dip-аналог). Только факты, владелец трактует."""
    posted = filler.posted
    filled_orders = [o for o in filler.orders if o.filled > 0]
    fully = [o for o in filler.orders if o.is_filled]
    qty_posted = sum(o.size for o in filler.orders)
    qty_filled = sum(o.filled for o in filler.orders)

    ts_sorted = [t.ts for t in trades]
    prices = [t.price for t in trades]
    markouts: list[float] = []
    for f in filler.fills:
        p_after = _price_at(ts_sorted, prices, f.ts + horizon_ms)
        if p_after <= 0 or f.price <= 0:
            continue
        mo = (p_after - f.price) if f.side == "buy" else (f.price - p_after)
        markouts.append(mo / f.price * 1e4)        # bps, со знаком
    adverse = [m for m in markouts if m < 0]
    pens = [o.penetration / o.price * 1e4 for o in filled_orders if o.price > 0]
    return {
        "posted": posted,
        "filled_orders": len(filled_orders),
        "fully_filled": len(fully),
        "fill_rate": round(len(filled_orders) / posted, 4) if posted else 0.0,
        "qty_fill_ratio": round(qty_filled / qty_posted, 4) if qty_posted > 0 else 0.0,
        "n_fills": len(filler.fills),
        "avg_markout_bps": round(sum(markouts) / len(markouts), 3) if markouts else None,
        "adverse_rate": round(len(adverse) / len(markouts), 4) if markouts else None,
        "avg_penetration_bps": round(sum(pens) / len(pens), 3) if pens else None,
    }


def book_queue_ahead(levels: list, level_price: float, side: str) -> float:
    """ОЦЕНКА объёма перед нами по агрегатному L2 (документированная граница): для buy —
    сумма bid-объёмов на ценах >= нашего уровня (более агрессивные биды стоят впереди);
    для sell — сумма ask-объёмов на ценах <= нашего уровня. levels: [[price,size]...]."""
    total = 0.0
    for p, q in levels:
        p = float(p)
        if (side == "buy" and p >= level_price - _EPS) or \
           (side == "sell" and p <= level_price + _EPS):
            total += float(q)
    return total
