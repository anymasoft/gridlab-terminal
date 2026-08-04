"""Фоновый архив живого L2-стакана и ленты сделок Bybit (блок E).

Пишет ПАРАЛЛЕЛЬНО с живой бумагой, НЕ мешая ей: записи кладутся в буфер O(1) из
колбэков фида (событийный цикл), а сброс на диск идёт в ОТДЕЛЬНОМ ПОТОКЕ
(asyncio.to_thread). Так накапливается история L2 для будущих исторических
бэктестов на стакане и для исследования order-flow сигналов.

ФОРМАТ — gzip-NDJSON по символу/дню, ротация по дню:
  <root>/orderbook/<SYMBOL>/<YYYY-MM-DD>.ndjson.gz   (snapshot + дельты)
  <root>/trades/<SYMBOL>/<YYYY-MM-DD>.ndjson.gz       (лента сделок)
Каждый сброс ДОПИСЫВАЕТ самостоятельный gzip-член (append) — файл всегда валиден и
читается целиком даже при жёстком обрыве процесса (crash-safe), без долгоживущих
файловых дескрипторов. Выбор gzip+json: нулевые новые зависимости (stdlib), хорошее
сжатие повторяющегося L2; parquet потребовал бы pyarrow (тяжёлая бинарная зависимость).

ЛЕНТА СДЕЛОК пишется ПОЛНО для анализа сигналов: точное время (мс), цена, объём,
СТОРОНА-агрессор (B=покупка/S=продажа). Этого достаточно, чтобы по архиву
реконструировать дисбаланс потока и интенсивность в любой момент T и сопоставить с
тем, что цена сделала ПОСЛЕ (см. order_flow / reconstruct_book).

Компактные ключи (экономия места):
  сделка:  {"t":ms, "p":цена, "v":объём, "s":"B"|"S"}
  стакан:  {"t":ms, "ty":"s"|"d", "b":[[p,sz]..], "a":[[p,sz]..], "u":updateId, "seq":seq}
"""
from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path


def _day(ms: int) -> str:
    return datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class ArchiveWriter:
    """Буфер в памяти + фоновый сброс в gzip-NDJSON. record_* вызывается из колбэков
    фида (без блокировки); flush() пишет на диск в отдельном потоке."""

    def __init__(self, root, flush_interval: float = 2.0) -> None:
        self.root = Path(root)
        self.flush_interval = flush_interval
        self._buf: list[tuple[str, str, str, str]] = []   # (stream, symbol, day, json-строка)
        self._stop = False
        self.records = 0
        self.flushes = 0

    # ── приём записей из живого фида (O(1), не блокирует цикл) ──
    def record_trade(self, symbol: str | None, tr) -> None:
        if not symbol:
            return
        rec = {"t": int(tr.ts), "p": tr.price, "v": tr.size,
               "s": "B" if tr.side == "Buy" else "S"}   # сторона-агрессор сохраняется
        self._buf.append(("trades", symbol, _day(tr.ts),
                          json.dumps(rec, separators=(",", ":"))))

    def record_book(self, symbol: str | None, msg: dict) -> None:
        if not symbol:
            return
        data = msg.get("data") or {}
        ts = int(msg.get("ts") or msg.get("cts") or 0)
        rec = {"t": ts, "ty": "s" if msg.get("type") == "snapshot" else "d",
               "b": data.get("b") or [], "a": data.get("a") or [],
               "u": data.get("u"), "seq": data.get("seq")}
        self._buf.append(("orderbook", symbol, _day(ts),
                          json.dumps(rec, separators=(",", ":"))))

    # ── фоновый сброс ──
    async def run(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    async def flush(self) -> None:
        if not self._buf:
            return
        batch = self._buf
        self._buf = []
        await asyncio.to_thread(self._write_batch, batch)   # запись на диск — в ОТДЕЛЬНОМ потоке

    def _write_batch(self, batch: list[tuple[str, str, str, str]]) -> None:
        groups: dict[tuple[str, str, str], list[str]] = {}
        for stream, symbol, day, line in batch:
            groups.setdefault((stream, symbol, day), []).append(line)
        for (stream, symbol, day), lines in groups.items():
            d = self.root / stream / symbol
            d.mkdir(parents=True, exist_ok=True)
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            with gzip.open(d / f"{day}.ndjson.gz", "ab") as gz:   # самостоятельный gzip-член (crash-safe)
                gz.write(payload)
            self.records += len(lines)
        self.flushes += 1

    def stop(self) -> None:
        self._stop = True

    def stats(self) -> dict:
        """Дёшево (без обхода диска) — для кадра UI."""
        return {"records": self.records, "flushes": self.flushes, "buffered": len(self._buf)}

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*.ndjson.gz"))


# ───────────────────────── чтение архива обратно ─────────────────────────
def read_ndjson_gz(path):
    """Генератор записей из gzip-NDJSON (прозрачно читает все дописанные gzip-члены)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def reconstruct_book(book_records, until_ms: int | None = None):
    """Восстановить книгу на момент времени из архивных записей стакана (snapshot+дельты).
    Это и есть основа будущего исторического бэктеста на стакане."""
    from .orderbook import OrderBookL2
    book = OrderBookL2()
    for r in book_records:
        if until_ms is not None and r.get("t", 0) > until_ms:
            break
        data = {"b": r.get("b") or [], "a": r.get("a") or [],
                "u": r.get("u"), "seq": r.get("seq"), "ts": r.get("t")}
        book.apply("snapshot" if r.get("ty") == "s" else "delta", data)
    return book


def order_flow(trade_records, t0: int, t1: int) -> dict:
    """Дисбаланс/интенсивность потока сделок в окне [t0, t1] из архивной ленты — пример
    реконструкции order-flow сигнала для исследования «есть ли в ленте предсказание»."""
    buy = sell = 0.0
    n = 0
    for r in trade_records:
        if t0 <= r.get("t", 0) <= t1:
            n += 1
            if r.get("s") == "B":
                buy += r.get("v", 0.0)
            else:
                sell += r.get("v", 0.0)
    tot = buy + sell
    return {"buy_vol": buy, "sell_vol": sell, "count": n,
            "imbalance": (buy - sell) / tot if tot > 0 else 0.0}
