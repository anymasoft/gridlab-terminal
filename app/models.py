"""Модели данных GRIDLAB. Pydantic для конфигов/ответов API; для горячих циклов
движка используем лёгкие dataclass'ы (скорость бэктеста)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─────────────────────────── рыночные данные ───────────────────────────
@dataclass
class Candle:
    ts: int     # начало бара, ms
    o: float
    h: float
    l: float
    c: float
    v: float


# ─────────────────────────── параметры стратегии ───────────────────────
class GridParams(BaseModel):
    """Безразмерные параметры адаптивного грида (защита от оверфита).
    Шаг = grid_spacing × ATR, TP/SL в ATR, размер ордера — доля капитала."""
    mode: Literal["heuristic", "avellaneda", "avellaneda_legacy", "manual"] = "avellaneda"
    grid_spacing: float = 1.8      # шаг сетки в единицах ATR (k)
    levels: int = 2                # уровней (2 = 1 buy + 1 sell, дефолт; >2 — многоуровневая сетка)
    order_usd: float = 80.0        # размер одного ордера в долларах (нотионал; крипто-режим)
    contract_qty: float = 0.0      # фьючерс: размер ордера в КОНТРАКТАХ (>0 → перекрывает order_usd; Replay = 1)
    tp: float = 0.0                # НЕ используется в Quoter-модели (round-trip через перекотировку)
    sl: float = 0.0                # stop-loss, ATR× (0 = выключен; ликвидация по марже остаётся)
    ema: int = 21                  # окно EMA-фильтра тренда
    max_orders: int = 10           # ЖЁСТКИЙ лимит инвентаря (единиц): набор стороны блокируется
                                   # в котировании И клипуется на исполнении (блок G)
    max_drawdown_pct: float = 0.0  # kill-switch: просадка счёта >= % → закрыть позицию и остановить
                                   # котирование (0 = выкл; настраиваемый порог риска, блок G)
    expansion: Literal["geometric", "arithmetic", "fibonacci"] = "geometric"
    atr_window: int = 14
    # Avellaneda-Stoikov (канонический, режим 'avellaneda'; формулы в avellaneda_canonical.py)
    as_gamma: float = 0.3          # риск-аверсия γ — главный регулятор риск/доходности
    as_kappa: float = 1000.0       # интенсивность κ: λ(δ)=A·e^(−κδ), δ в ДОЛЯХ цены.
                                   # ВРЕМЕННЫЙ дефолт — калибруется из реального потока сделок (блок C, fit_intensity).
    as_horizon: int = 200          # горизонт T (бары); стационарный скользящий горизонт (T−t)≡T
    session_pause: int = 0         # мин. до конца/после начала торгового дня — закрыть позу и не торговать (0=выкл)


# ─────────────────────────── ордера / сделки ───────────────────────────
Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]
OrderStatus = Literal["open", "filled", "cancelled"]


@dataclass
class Order:
    id: int
    side: Side
    price: float
    size: float            # в базовой валюте (контрактах)
    otype: OrderType = "limit"
    status: OrderStatus = "open"
    level: int = 0         # номер уровня сетки (для метки G1..Gn)
    ts: int = 0


@dataclass
class Fill:
    order_id: int
    side: Side
    price: float
    size: float
    fee: float             # абсолютная комиссия, $ (>0 = уплачено)
    ts: int
    is_maker: bool
    note: str = ""


@dataclass
class LogEvent:
    ts: int
    time: str
    action: str
    sym: str
    price: str
    size: str
    fee: str
    pnl: str
    comment: str


# ─────────────────────────── позиция инструмента ───────────────────────
@dataclass
class Position:
    qty: float = 0.0        # >0 лонг, <0 шорт (нетто)
    avg_entry: float = 0.0
    realized: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0

    def unrealized(self, mark: float) -> float:
        return self.qty * (mark - self.avg_entry)


# ─────────────────────────── результаты прогона ─────────────────────────
@dataclass
class InstrumentResult:
    sym: str
    alloc: float
    pnl: float
    pnl_pct: float
    equity_curve: list[float]
    spark: list[float]
    orders_open: int
    status: str             # 'active' | 'stop'
    trades: int
    fees: float
    funding: float
    liquidated: bool


class RunRequest(BaseModel):
    symbols: Optional[list[str]] = None
    interval: Optional[str] = None
    params: Optional[GridParams] = None
    per_symbol: Optional[dict[str, GridParams]] = None  # переопределение на инструмент


class SweepRequest(BaseModel):
    symbol: str
    interval: Optional[str] = None
    k_min: float = 0.5
    k_max: float = 3.5
    k_step: float = 0.25
    base: Optional[GridParams] = None


# ─────────────────────── Optimization Lab (бэктест) ───────────────────────
class LabRequest(BaseModel):
    """Запрос к бэктест-лаборатории. source='local' → ETHBTC.txt (спот, без funding);
    'bybit' → перп-инструмент по API."""
    source: Literal["local", "bybit"] = "local"
    symbol: str = "ETHBTC"
    tf: str = "15m"
    bars: int = 4000               # сколько последних свечей взять
    capital: float = 1000.0
    base: Optional[GridParams] = None
    grid: Optional[dict[str, list[float]]] = None   # {param: [значения]}
    weights: Optional[dict[str, float]] = None       # робастная метрика
    # heatmap
    xkey: Optional[str] = None
    xvals: Optional[list[float]] = None
    ykey: Optional[str] = None
    yvals: Optional[list[float]] = None
    metric: str = "score"
    # walk-forward
    train_days: int = 30
    test_days: int = 7
    max_windows: int = 10
