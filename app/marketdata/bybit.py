"""Доступ к рыночным данным Bybit v5 (keyless для публичных эндпоинтов).

Используется ОДИН источник истины по ценам — реальные свечи Bybit, чтобы
бумажному прогону можно было доверять. Переключатель mainnet/testnet — через
VENUE в .env (testnet отдаёт те же публичные klines).
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import Settings, get_settings
from ..models import Candle

log = logging.getLogger("gridlab.bybit")

# Bybit принимает интервалы в минутах: 1,3,5,15,30,60,120,240,360,720, D,W,M
_VALID = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}


def _norm_interval(interval: str) -> str:
    interval = str(interval).strip()
    return interval if interval in _VALID else "15"


async def fetch_klines(symbol: str, interval: str, limit: int,
                       settings: Settings | None = None) -> list[Candle]:
    """Свечи по символу, по возрастанию времени (старые → новые).

    Bybit отдаёт максимум 1000 за запрос и в порядке новые→старые — переворачиваем.
    """
    s = settings or get_settings()
    interval = _norm_interval(interval)
    limit = max(1, min(int(limit), 1000))
    url = f"{s.active_base_url}/v5/market/kline"
    params = {"category": s.bybit_category, "symbol": symbol,
              "interval": interval, "limit": limit}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit klines {symbol}: {data.get('retMsg')}")
    rows = data["result"]["list"]
    out: list[Candle] = []
    for row in rows:
        out.append(Candle(ts=int(row[0]), o=float(row[1]), h=float(row[2]),
                          l=float(row[3]), c=float(row[4]), v=float(row[5])))
    out.sort(key=lambda c: c.ts)
    return out


async def fetch_funding(symbol: str, limit: int = 200,
                        settings: Settings | None = None) -> dict[int, float]:
    """История funding-ставок: {ts_ms: rate}. Для honest-стоимости удержания инвентаря.
    При ошибке — пустой словарь (движок применит дефолтную ставку)."""
    s = settings or get_settings()
    url = f"{s.active_base_url}/v5/market/funding/history"
    params = {"category": s.bybit_category, "symbol": symbol, "limit": min(limit, 200)}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        if data.get("retCode") != 0:
            return {}
        return {int(x["fundingRateTimestamp"]): float(x["fundingRate"])
                for x in data["result"]["list"]}
    except Exception as e:  # noqa: BLE001
        log.info("funding fetch failed %s: %s", symbol, e)
        return {}


async def fetch_instrument_meta(symbol: str,
                                settings: Settings | None = None) -> dict:
    """tickSize/qtyStep/minOrderQty для аккуратного округления (как на бирже)."""
    s = settings or get_settings()
    url = f"{s.active_base_url}/v5/market/instruments-info"
    params = {"category": s.bybit_category, "symbol": symbol}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        item = data["result"]["list"][0]
        return {
            "tick_size": float(item["priceFilter"]["tickSize"]),
            "qty_step": float(item["lotSizeFilter"]["qtyStep"]),
            "min_qty": float(item["lotSizeFilter"]["minOrderQty"]),
        }
    except Exception as e:  # noqa: BLE001
        log.info("meta fetch failed %s: %s", symbol, e)
        return {"tick_size": 0.0, "qty_step": 0.0, "min_qty": 0.0}


async def fetch_orderbook(symbol: str, depth: int = 16,
                          settings: Settings | None = None) -> dict:
    """Стакан Bybit: {'a': [[price,size]...] аски ↑, 'b': [[price,size]...] биды ↓}."""
    s = settings or get_settings()
    url = f"{s.active_base_url}/v5/market/orderbook"
    params = {"category": s.bybit_category, "symbol": symbol, "limit": 50}
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        d = r.json()
    res = d.get("result", {})
    a = [[float(p), float(q)] for p, q in res.get("a", [])][:depth]
    b = [[float(p), float(q)] for p, q in res.get("b", [])][:depth]
    return {"a": a, "b": b}


async def fetch_many_klines(symbols: list[str], interval: str, limit: int,
                            settings: Settings | None = None
                            ) -> dict[str, list[Candle]]:
    """Параллельная загрузка свечей по корзине инструментов."""
    async def one(sym: str) -> tuple[str, list[Candle]]:
        try:
            return sym, await fetch_klines(sym, interval, limit, settings)
        except Exception as e:  # noqa: BLE001
            log.warning("klines failed %s: %s", sym, e)
            return sym, []
    pairs = await asyncio.gather(*(one(s) for s in symbols))
    return {sym: cs for sym, cs in pairs if cs}


_WS_MAINNET = "wss://stream.bybit.com/v5/public/linear"
_WS_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


async def stream_tickers(symbols: list[str], settings: Settings | None = None):
    """Async-генератор реальных тиков Bybit: yield (symbol, last_price, ts_ms).
    Одно подключение на всю корзину (topics tickers.<symbol>). Сабсредства — keyless."""
    import json
    import websockets  # из uvicorn[standard]

    s = settings or get_settings()
    url = _WS_TESTNET if s.venue == "testnet" else _WS_MAINNET
    args = [f"tickers.{sym}" for sym in symbols]
    async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                  max_queue=256) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        async for raw in ws:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            topic = d.get("topic", "")
            if not topic.startswith("tickers."):
                continue
            data = d.get("data") or {}
            lp = data.get("lastPrice") or data.get("markPrice")
            sym = data.get("symbol") or topic.split(".", 1)[-1]
            if lp:
                try:
                    yield sym, float(lp), int(d.get("ts", 0)) or now_ms()
                except (TypeError, ValueError):
                    continue


async def stream_public_ws(topics: list[str], settings: Settings | None = None):
    """Низкоуровневый поток публичного WS Bybit v5 (linear): подписка на произвольные
    topics (например orderbook.50.<sym>, publicTrade.<sym>), yield РАСПАРСЕННЫХ
    сообщений (dict). Реконнект/бэк-офф — на стороне вызывающего (как у stream_tickers
    + _tick_loop): при разрыве генератор завершается, вызывающий переподключается и
    заново шлёт subscribe. Keyless (публичные данные)."""
    import json
    import websockets  # из uvicorn[standard]

    s = settings or get_settings()
    url = _WS_TESTNET if s.venue == "testnet" else _WS_MAINNET
    async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                  max_queue=512) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": list(topics)}))
        async for raw in ws:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            yield d


def latest_price(candles: list[Candle]) -> float:
    return candles[-1].c if candles else 0.0


def now_ms() -> int:
    return int(time.time() * 1000)
