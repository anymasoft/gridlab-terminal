"""Загрузчик локальных котировок (Finam-CSV) для бэктест-лаборатории.

Файл `ETHBTC.txt`: <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>
PER=1 → 1-минутные свечи. ETHBTC — СПОТ-пара (не перп) → funding неприменим.

Читаем файл ОДИН раз (только свежий участок от FLOOR_DATE — ранние 2017 плоские/VOL=0),
держим компактные кортежи в памяти, ресэмплим в нужный ТФ на лету. Это исследовательский
бэктест на истории, не realtime.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..models import Candle

# по умолчанию грузим с 2024 — ранние данные неликвидны (плоские 0.08, VOL=0)
FLOOR_DATE = "20240101"

_TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
          "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# кэш сырых 1m баров: {path: [(ts_ms, o,h,l,c,v), ...]}
_RAW: dict[str, list[tuple]] = {}
_META: dict[str, dict] = {}


def _default_path() -> str:
    # ETHBTC.txt лежит в корне D:\DEFI (рядом с gridlab)
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../gridlab
    root = os.path.dirname(here)  # .../DEFI
    return os.path.join(root, "ETHBTC.txt")


def _parse_ts(date_s: str, time_s: str) -> int:
    # DATE=YYYYMMDD, TIME=HHMMSS (трактуем как UTC)
    time_s = time_s.zfill(6)
    dt = datetime(int(date_s[0:4]), int(date_s[4:6]), int(date_s[6:8]),
                  int(time_s[0:2]), int(time_s[2:4]), int(time_s[4:6]), tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _load_raw(path: str, floor_date: str = FLOOR_DATE) -> list[tuple]:
    if path in _RAW:
        return _RAW[path]
    rows: list[tuple] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.readline()  # заголовок
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 9:
                continue
            date_s, time_s = p[2], p[3]
            if date_s < floor_date:
                continue
            try:
                ts = _parse_ts(date_s, time_s)
                rows.append((ts, float(p[4]), float(p[5]), float(p[6]), float(p[7]), float(p[8])))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda r: r[0])
    _RAW[path] = rows
    if rows:
        _META[path] = {"n": len(rows), "from": rows[0][0], "to": rows[-1][0]}
    return rows


def _resample(raw: list[tuple], tf_ms: int) -> list[Candle]:
    out: list[Candle] = []
    cur_bucket = -1
    o = h = l = c = v = 0.0
    for ts, ro, rh, rl, rc, rv in raw:
        b = (ts // tf_ms) * tf_ms
        if b != cur_bucket:
            if cur_bucket >= 0:
                out.append(Candle(ts=cur_bucket, o=o, h=h, l=l, c=c, v=v))
            cur_bucket = b
            o, h, l, c, v = ro, rh, rl, rc, rv
        else:
            h = max(h, rh)
            l = min(l, rl)
            c = rc
            v += rv
    if cur_bucket >= 0:
        out.append(Candle(ts=cur_bucket, o=o, h=h, l=l, c=c, v=v))
    return out


def get_candles(tf: str = "5m", start_ms: int | None = None, end_ms: int | None = None,
                path: str | None = None, limit: int | None = None) -> list[Candle]:
    """Свечи локального файла в заданном ТФ за период [start_ms, end_ms]."""
    path = path or _default_path()
    tf_ms = _TF_MS.get(tf, 300_000)
    raw = _load_raw(path)
    if start_ms is not None or end_ms is not None:
        s = start_ms if start_ms is not None else -1
        e = end_ms if end_ms is not None else 1 << 62
        raw = [r for r in raw if s <= r[0] <= e]
    candles = _resample(raw, tf_ms)
    if limit:
        candles = candles[-limit:]
    return candles


# ───────── потоковое чтение для Replay (без загрузки всего архива в память) ─────────
_LINECOUNT: dict[str, tuple[int, int]] = {}   # path -> (mtime, кол-во строк данных)


def count_data_lines(path: str) -> int:
    """Быстрый подсчёт строк данных (минус заголовок) — для оценки длины ряда. Кешируется по mtime."""
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        return 0
    cached = _LINECOUNT.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    n = max(0, n - 1)
    _LINECOUNT[path] = (mtime, n)
    return n


def byte_offset_for_ts(path: str, target_ms: int) -> int:
    """Бинарный поиск по БАЙТАМ файла: смещение строки, чьё время ≈ target_ms (чуть ниже).
    Файл отсортирован по времени → O(log размер), без полного чтения."""
    with open(path, "rb") as f:
        f.readline()                                   # заголовок
        data_start = f.tell()
        f.seek(0, os.SEEK_END)
        hi = f.tell()
        lo = data_start
        while hi - lo > 256:
            mid = (lo + hi) // 2
            f.seek(mid)
            f.readline()                               # выровнять к началу следующей строки
            start = f.tell()
            line = f.readline().decode("utf-8", "ignore")
            if not line:
                hi = mid
                continue
            p = line.split(",")
            try:
                ts = _parse_ts(p[2], p[3])
            except (ValueError, IndexError):
                lo = start + max(1, len(line))
                continue
            if ts < target_ms:
                lo = start
            else:
                hi = start
        return lo


def history_chunk(path: str, tf: str, before_ms: int, count: int) -> list[Candle]:
    """До `count` ресэмпл-свечей СТРОГО раньше before_ms — для прокрутки графика назад.
    Находим позицию бинарным поиском и читаем небольшой кусок потоком (без всего архива)."""
    tf_ms = _TF_MS.get(tf, 300_000)
    count = max(1, min(count, 1500))
    before_bucket = (before_ms // tf_ms) * tf_ms
    start_ts = before_ms - count * tf_ms * 3           # с запасом на торговые гэпы
    off = byte_offset_for_ts(path, start_ts)
    out: list[Candle] = []
    cur = -1
    o = h = l = c = v = 0.0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(off)
        if off > 0:
            f.readline()                               # выровнять
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 9:
                continue
            try:
                ts = _parse_ts(p[2], p[3])
                ro, rh, rl, rc, rv = float(p[4]), float(p[5]), float(p[6]), float(p[7]), float(p[8])
            except (ValueError, IndexError):
                continue
            b = (ts // tf_ms) * tf_ms
            if b != cur:
                if 0 <= cur < before_bucket:
                    out.append(Candle(ts=cur, o=o, h=h, l=l, c=c, v=v))
                    if len(out) > count:
                        out.pop(0)
                if b >= before_bucket:
                    break
                cur, o, h, l, c, v = b, ro, rh, rl, rc, rv
            else:
                h = max(h, rh)
                l = min(l, rl)
                c = rc
                v += rv
    return out


def future_chunk(path: str, tf: str, after_ms: int, count: int) -> list[Candle]:
    """До `count` ресэмпл-свечей СТРОГО позже after_ms — для прокрутки графика вперёд
    (предпросмотр будущих свечей архива). Бинарный поиск позиции + чтение куска потоком."""
    tf_ms = _TF_MS.get(tf, 300_000)
    count = max(1, min(count, 1500))
    after_bucket = (after_ms // tf_ms) * tf_ms
    off = byte_offset_for_ts(path, after_ms)
    out: list[Candle] = []
    cur = -1
    o = h = l = c = v = 0.0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(off)
        if off > 0:
            f.readline()                               # выровнять
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 9:
                continue
            try:
                ts = _parse_ts(p[2], p[3])
                ro, rh, rl, rc, rv = float(p[4]), float(p[5]), float(p[6]), float(p[7]), float(p[8])
            except (ValueError, IndexError):
                continue
            b = (ts // tf_ms) * tf_ms
            if b != cur:
                if cur > after_bucket:                 # завершён бар строго позже точки
                    out.append(Candle(ts=cur, o=o, h=h, l=l, c=c, v=v))
                    if len(out) >= count:
                        return out
                cur, o, h, l, c, v = b, ro, rh, rl, rc, rv
            else:
                h = max(h, rh)
                l = min(l, rl)
                c = rc
                v += rv
        if cur > after_bucket:
            out.append(Candle(ts=cur, o=o, h=h, l=l, c=c, v=v))
    return out


def first_last_ts(path: str) -> tuple[int, int]:
    """Время первого и последнего бара файла (ms) — дёшево (без полного чтения)."""
    try:
        with open(path, "rb") as f:
            f.readline()                               # заголовок
            first = f.readline().decode("utf-8", "ignore")
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 8192)
            f.seek(size - back)
            tail = f.read().decode("utf-8", "ignore")
        last = [ln for ln in tail.splitlines() if ln.strip()][-1]

        def ts(line: str) -> int:
            p = line.split(",")
            return _parse_ts(p[2], p[3])
        return ts(first), ts(last)
    except Exception:
        return 0, 0


def stream_candles(path: str, tf: str, skip_lines: int = 0):
    """Генератор ресэмпл-свечей ТФ из файла, начиная ПОСЛЕ skip_lines строк данных.
    Память O(1): не держим весь архив, читаем поток. Для Replay (играем вперёд по окну)."""
    tf_ms = _TF_MS.get(tf, 300_000)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.readline()                       # заголовок
        for _ in range(max(0, skip_lines)):
            if not f.readline():
                return
        cur_bucket = -1
        o = h = l = c = v = 0.0
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 9:
                continue
            try:
                ts = _parse_ts(p[2], p[3])
                ro, rh, rl, rc, rv = float(p[4]), float(p[5]), float(p[6]), float(p[7]), float(p[8])
            except (ValueError, IndexError):
                continue
            b = (ts // tf_ms) * tf_ms
            if b != cur_bucket:
                if cur_bucket >= 0:
                    yield Candle(ts=cur_bucket, o=o, h=h, l=l, c=c, v=v)
                cur_bucket, o, h, l, c, v = b, ro, rh, rl, rc, rv
            else:
                h = max(h, rh)
                l = min(l, rl)
                c = rc
                v += rv
        if cur_bucket >= 0:
            yield Candle(ts=cur_bucket, o=o, h=h, l=l, c=c, v=v)


def raw_series(path: str, floor_date: str = "20090101") -> list[tuple]:
    """Сырые 1m бары (ts_ms,o,h,l,c,v) файла — для проигрывания в Replay тик-за-тиком.
    floor_date по умолчанию пускает всю историю (фьючерсы ликвидны с 2009)."""
    key = path + "|" + floor_date
    if key in _RAW:
        return _RAW[key]
    rows = _load_raw(path, floor_date) if floor_date == FLOOR_DATE else None
    if rows is None:
        rows = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.readline()
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) < 9 or p[2] < floor_date:
                    continue
                try:
                    rows.append((_parse_ts(p[2], p[3]), float(p[4]), float(p[5]),
                                 float(p[6]), float(p[7]), float(p[8])))
                except (ValueError, IndexError):
                    continue
        rows.sort(key=lambda r: r[0])
    _RAW[key] = rows
    if rows:
        _META[key] = {"n": len(rows), "from": rows[0][0], "to": rows[-1][0]}
    return rows


_RESAMPLE_CACHE: dict[tuple, list[Candle]] = {}


def resample_raw(raw: list[tuple], tf: str, cache_key: str | None = None) -> list[Candle]:
    """Публичная обёртка ресэмпла сырых баров в нужный ТФ (для Replay-графика).
    cache_key (напр. путь файла) → кэшируем результат, чтобы рестарт не пересчитывал всё."""
    if cache_key is not None:
        ck = (cache_key, tf, len(raw))
        cached = _RESAMPLE_CACHE.get(ck)
        if cached is not None:
            return cached
        out = _resample(raw, _TF_MS.get(tf, 60_000))
        _RESAMPLE_CACHE[ck] = out
        return out
    return _resample(raw, _TF_MS.get(tf, 60_000))


def file_info(path: str, symbol: str, floor_date: str = "20090101") -> dict:
    """Метаданные произвольного архива (период/объём)."""
    if not os.path.exists(path):
        return {"exists": False, "path": path, "symbol": symbol}
    raw = raw_series(path, floor_date)
    key = path + "|" + floor_date
    m = _META.get(key, {})
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else "—"
    return {
        "exists": True, "path": path, "symbol": symbol, "base_tf": "1m",
        "bars_1m": m.get("n", len(raw)),
        "from": fmt(m.get("from")), "to": fmt(m.get("to")),
        "from_ms": m.get("from"), "to_ms": m.get("to"),
    }


def info(path: str | None = None) -> dict:
    """Метаданные файла: путь, число 1m баров, период."""
    path = path or _default_path()
    exists = os.path.exists(path)
    if not exists:
        return {"exists": False, "path": path}
    raw = _load_raw(path)
    m = _META.get(path, {})
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else "—"
    return {
        "exists": True, "path": path, "symbol": "ETHBTC", "base_tf": "1m",
        "is_spot": True, "funding": False,
        "bars_1m": m.get("n", len(raw)),
        "from": fmt(m.get("from")), "to": fmt(m.get("to")),
        "from_ms": m.get("from"), "to_ms": m.get("to"),
        "note": "Спот ETHBTC — funding неприменим; грузим с " + FLOOR_DATE,
    }
