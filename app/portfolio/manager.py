"""Портфельный оркестратор: ОДНА адаптивная grid-стратегия на КОРЗИНЕ ~10
инструментов. $1000 распределены risk-parity (∝ доле, обратно к волатильности).
Каждый инструмент — независимый PaperEngine; портфельный эквити = сумма."""
from __future__ import annotations

import statistics

from ..analytics import metrics
from ..config import Settings
from ..engine.costmodel import CostModel
from ..engine.paper import PaperEngine
from ..models import Candle, GridParams
from ..strategy.indicators import Indicators

_COLORS = {
    "buy": "#157a4f", "sell": "#a93529", "liq": "#C4453A",
    "info": "#3B82CE", "neutral": "#5b635e",
}


def _action_color(action: str) -> str:
    if "Buy" in action:
        return _COLORS["buy"]
    if "Sell" in action:
        return _COLORS["sell"]
    if "Ликвид" in action:
        return _COLORS["liq"]
    if action in ("Шаг сетки ↑", "Шаг сетки ↓", "Старт", "Funding"):
        return _COLORS["info"]
    if "Stop" in action:
        return _COLORS["liq"]
    return _COLORS["neutral"]


def _vol_of(candles: list[Candle]) -> float:
    """Грубая волатильность инструмента = ATR%/цена за окно — для risk-parity."""
    if len(candles) < 15:
        return 0.01
    ind = Indicators()
    for c in candles[-60:]:
        ind.update(c)
    px = candles[-1].c
    return max(ind.atr / px, 1e-4) if px > 0 else 0.01


def risk_parity_alloc(candles_map: dict[str, list[Candle]], capital: float
                      ) -> dict[str, float]:
    """Вес ∝ 1/vol (риск-паритет): волатильные инструменты получают меньше денег."""
    inv = {s: 1.0 / _vol_of(cs) for s, cs in candles_map.items()}
    total = sum(inv.values()) or 1.0
    return {s: round(capital * w / total, 2) for s, w in inv.items()}


def _downsample(xs: list[float], n: int = 22) -> list[float]:
    if len(xs) <= n:
        return xs[:]
    step = len(xs) / n
    return [xs[min(int(i * step), len(xs) - 1)] for i in range(n)]


def _fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def run_portfolio(candles_map: dict[str, list[Candle]], settings: Settings,
                  base: GridParams, per_symbol: dict[str, GridParams] | None = None,
                  funding_map: dict[str, dict[int, float]] | None = None,
                  interval: str = "15",
                  specs: dict[str, dict] | None = None) -> dict:
    per_symbol = per_symbol or {}
    funding_map = funding_map or {}
    specs = specs or {}
    cost = CostModel.from_settings(settings)
    allocs = risk_parity_alloc(candles_map, settings.start_capital)

    engines: dict[str, PaperEngine] = {}
    for sym, candles in candles_map.items():
        p = per_symbol.get(sym, base)
        eng = PaperEngine(sym, allocs[sym], p, cost, funding_map.get(sym),
                          spec=specs.get(sym))
        eng.run(candles)
        engines[sym] = eng

    # ── агрегированная кривая эквити (выровнять по числу баров) ──
    min_len = min((len(e.equity_curve) for e in engines.values()), default=0)
    port_curve: list[float] = []
    for i in range(min_len):
        port_curve.append(sum(e.equity_curve[i] for e in engines.values()))

    # ── инструменты ──
    instruments = []
    all_trade_pnls: list[float] = []
    total_equity = total_realized = total_unreal = total_fees = total_funding = 0.0
    open_orders = 0
    for sym, e in engines.items():
        s = e.summary()
        all_trade_pnls.extend(e.realized_pnls)
        unreal = e.pos.unrealized(e._last_price)
        total_equity += s["equity"]
        total_realized += s["realized"]
        total_unreal += unreal
        total_fees += s["fees"]
        total_funding += s["funding"]
        open_orders += s["orders_open"]
        instruments.append({
            "sym": sym,
            "alloc": s["alloc"],
            "pnl": s["pnl"],
            "pnl_pct": s["pnl_pct"],
            "orders": s["orders_open"],
            "status": ("stop" if s["liquidated"]
                       else "blocked" if s.get("blocked") else "active"),
            "blocked": s.get("blocked", ""),
            "min_order_usd": s.get("min_order_usd", 0.0),
            "trades": s["trades"],
            "spark": _downsample(e.equity_curve),
            "fills": [{"ts": f.ts, "side": f.side, "price": f.price}
                      for f in e.fills if f.is_maker][-60:],
        })

    balance = sum(e.cash for e in engines.values())
    equity = total_equity
    roi = (equity - settings.start_capital) / settings.start_capital * 100.0

    # max DD портфеля
    peak, max_dd = (port_curve[0] if port_curve else 0.0), 0.0
    for v in port_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    # ── журнал событий (слить, новые сверху) ──
    raw = []
    for sym, e in engines.items():
        for ev in e.events:
            raw.append((ev, sym))
    raw.sort(key=lambda x: x[0].ts, reverse=True)
    log = []
    for ev, sym in raw[:200]:
        log.append({
            "ts": ev.ts,
            "action": ev.action,
            "actionColor": _action_color(ev.action),
            "sym": sym,
            "price": _fmt_price(ev.price) if ev.price else "—",
            "size": (f"{ev.size:.4f}".rstrip("0").rstrip(".")) if ev.size else "—",
            "fee": f"-${ev.fee:.2f}" if ev.fee else "—",
            "pnl": (("+" if ev.pnl >= 0 else "-") + f"${abs(ev.pnl):.2f}") if ev.pnl else "—",
            "pnlColor": "#157a4f" if ev.pnl > 0 else ("#a93529" if ev.pnl < 0 else "#939a93"),
            "comment": ev.comment,
        })

    analytics = metrics.compute(port_curve, all_trade_pnls, interval)

    return {
        "balance": round(balance, 2),
        "equity": round(equity, 2),
        "roi": round(roi, 2),
        "drawdown": round(max_dd * 100.0, 2),
        "realized": round(total_realized, 2),
        "unrealized": round(total_unreal, 2),
        "open_orders": open_orders,
        "fees": round(total_fees, 2),
        "funding": round(total_funding, 2),
        "equity_curve": [round(v, 2) for v in port_curve],
        "instruments": instruments,
        "log": log,
        "analytics": analytics,
        "interval": interval,
        "start_capital": settings.start_capital,
    }
