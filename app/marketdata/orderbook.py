"""Живой L2-стакан Bybit (блок B): локальная книга bid/ask из WS snapshot + дельт,
плюс поток реальных сделок (publicTrade). Книга и лента питают:
  - калибровку κ канонического A-S (блок A ждёт реальный поток сделок);
  - честный эмулятор исполнения по trade-through (блок C);
  - визуализацию стакана и ленту сделок (подключим по дизайну ПОСЛЕ блоков B/C).

Реконнект/бэк-офф с переподпиской и ВОССТАНОВЛЕНИЕМ книги (после разрыва первый
кадр WS — snapshot, книга отстраивается заново) — как в session._tick_loop.

Keyless: публичные топики Bybit, приватные эндпоинты для бумаги не нужны.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass

from ..config import Settings
from . import bybit


@dataclass
class Trade:
    """Реальная сделка из publicTrade. side — сторона тейкера (агрессора)."""
    ts: int          # время сделки, ms
    price: float
    size: float
    side: str        # 'Buy' | 'Sell' (тейкер)
    trade_id: str = ""


class OrderBookL2:
    """Локальная книга: price -> size по каждой стороне. Дельта с size==0 удаляет
    уровень (соглашение Bybit v5)."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.update_id: int = 0
        self.seq: int = 0
        self.ts: int = 0

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.update_id = 0
        self.seq = 0

    def apply_snapshot(self, data: dict) -> None:
        self.bids = {float(p): float(q) for p, q in data.get("b", [])}
        self.asks = {float(p): float(q) for p, q in data.get("a", [])}
        self.update_id = int(data.get("u", 0))
        self.seq = int(data.get("seq", 0))

    def apply_delta(self, data: dict) -> None:
        for p, q in data.get("b", []):
            fp, fq = float(p), float(q)
            if fq == 0.0:
                self.bids.pop(fp, None)
            else:
                self.bids[fp] = fq
        for p, q in data.get("a", []):
            fp, fq = float(p), float(q)
            if fq == 0.0:
                self.asks.pop(fp, None)
            else:
                self.asks[fp] = fq
        self.update_id = int(data.get("u", self.update_id))
        self.seq = int(data.get("seq", self.seq))

    def apply(self, msg_type: str, data: dict) -> None:
        """Bybit: type=='snapshot' заменяет книгу, 'delta' инкрементально обновляет.
        После реконнекта первый кадр — snapshot, поэтому книга восстанавливается сама."""
        if msg_type == "snapshot" or not (self.bids or self.asks):
            self.apply_snapshot(data)
        else:
            self.apply_delta(data)
        self.ts = int(data.get("ts", self.ts)) or self.ts

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def mid(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2.0 if bb and ba else 0.0

    @property
    def spread(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        return (ba - bb) if (bb and ba) else 0.0

    def is_valid(self) -> bool:
        """Книга пригодна: есть обе стороны и нет пересечения (bid<ask)."""
        return bool(self.bids and self.asks and self.best_bid < self.best_ask)

    def top(self, n: int = 16) -> dict:
        """Топ-n уровней. Формат совместим с fetch_orderbook: {'a':[[p,q]..]↑, 'b':[[p,q]..]↓}."""
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return {"a": [[p, q] for p, q in asks], "b": [[p, q] for p, q in bids]}


class OrderBookFeed:
    """Единый живой поток L2-стакана + сделок по ОДНОМУ символу (выбранному в UI).
    Держит локальную книгу, кольцевой буфер сделок и статистику (частота обновлений,
    реконнекты). Сделки рассылаются подписчикам через on_trade (потребители — κ-калибровка
    блока A и эмулятор блока C). Реконнект с экспоненциальным бэк-оффом и переподпиской."""

    def __init__(self, settings: Settings, depth: int = 50, trades_maxlen: int = 4000,
                 backoff_base: float = 1.0, backoff_max: float = 10.0) -> None:
        self.s = settings
        self.depth = depth
        self.book = OrderBookL2()
        self.trades: deque[Trade] = deque(maxlen=trades_maxlen)
        self.symbol: str | None = None
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # статистика
        self.connected = False
        self.updates = 0
        self.trade_count = 0
        self.reconnects = 0
        self.last_msg_wall = 0.0
        self.update_rate = 0.0       # обновлений книги в секунду (скользящее окно ~1с)
        self._rate_n = 0
        self._rate_t = time.monotonic()
        self._stop = False
        self._trade_cbs: list = []
        self._book_cbs: list = []

    # ── подписка потребителей на сделки (κ-калибровка / эмулятор очереди) ──
    def on_trade(self, cb) -> None:
        self._trade_cbs.append(cb)

    # ── подписка на сырые сообщения стакана (snapshot/delta) — для архива (блок E) ──
    def on_book(self, cb) -> None:
        self._book_cbs.append(cb)

    # ── срезы для UI/потребителей ──
    def snapshot(self, n: int = 16) -> dict:
        return self.book.top(n)

    def recent_trades(self, n: int = 50) -> list[Trade]:
        return list(self.trades)[-n:]

    def stats(self) -> dict:
        return {
            "symbol": self.symbol,
            "connected": self.connected,
            "bids": len(self.book.bids),
            "asks": len(self.book.asks),
            "best_bid": round(self.book.best_bid, 6),
            "best_ask": round(self.book.best_ask, 6),
            "mid": round(self.book.mid, 6),
            "spread": round(self.book.spread, 6),
            "updates": self.updates,
            "update_rate": round(self.update_rate, 1),
            "trades": self.trade_count,
            "reconnects": self.reconnects,
            "age_s": round(time.time() - self.last_msg_wall, 2) if self.last_msg_wall else None,
        }

    def stop(self) -> None:
        self._stop = True

    def _dispatch(self, d: dict) -> None:
        topic = d.get("topic", "")
        if topic.startswith("orderbook."):
            self.book.apply(d.get("type", ""), d.get("data") or {})
            self.updates += 1
            self._rate_n += 1
            now = time.monotonic()
            el = now - self._rate_t
            if el >= 1.0:
                self.update_rate = self._rate_n / el
                self._rate_n = 0
                self._rate_t = now
            self.last_msg_wall = time.time()
            for cb in self._book_cbs:                  # архив стакана (блок E)
                with contextlib.suppress(Exception):
                    cb(d)
        elif topic.startswith("publicTrade."):
            for t in (d.get("data") or []):
                try:
                    tr = Trade(ts=int(t.get("T", 0)), price=float(t.get("p", 0)),
                               size=float(t.get("v", 0)), side=str(t.get("S", "")),
                               trade_id=str(t.get("i", "")))
                except (TypeError, ValueError):
                    continue
                self.trades.append(tr)
                self.trade_count += 1
                for cb in self._trade_cbs:
                    with contextlib.suppress(Exception):
                        cb(tr)
            self.last_msg_wall = time.time()

    async def run(self, current_symbol) -> None:
        """Главный цикл. current_symbol() -> символ для отслеживания (или None).
        При смене символа — переподписка; при разрыве — бэк-офф и восстановление книги."""
        backoff = self.backoff_base
        while not self._stop:
            sym = current_symbol() if callable(current_symbol) else current_symbol
            if not sym:
                await asyncio.sleep(0.3)
                continue
            self.symbol = sym
            self.book.clear()
            self.connected = False
            topics = [f"orderbook.{self.depth}.{sym}", f"publicTrade.{sym}"]
            agen = bybit.stream_public_ws(topics, self.s)
            try:
                async for d in agen:
                    if self._stop or (callable(current_symbol) and current_symbol() != sym):
                        break
                    self._dispatch(d)
                    self.connected = True
                    backoff = self.backoff_base       # успешный кадр сбрасывает бэк-офф
            except Exception:
                self.connected = False
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self.backoff_max)
            finally:
                with contextlib.suppress(Exception):
                    await agen.aclose()                # закрыть ws при break/ошибке
