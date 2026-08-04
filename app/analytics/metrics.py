"""Метрики эффективности: Sharpe/Sortino/Profit Factor/Win Rate/Max DD/Avg Trade.
Считаются из портфельной кривой эквити (по барам) и реализованных PnL сделок."""
from __future__ import annotations

import math

_BARS_PER_YEAR = {
    "1": 525600, "5": 105120, "15": 35040, "30": 17520,
    "60": 8760, "120": 4380, "240": 2190, "D": 365,
}


def _ann_factor(interval: str) -> float:
    return math.sqrt(_BARS_PER_YEAR.get(str(interval), 35040))


def compute(equity_curve: list[float], trade_pnls: list[float],
            interval: str = "15") -> dict:
    n = len(equity_curve)
    if n < 2:
        return _empty()

    rets = []
    for i in range(1, n):
        prev = equity_curve[i - 1]
        if prev > 0:
            rets.append((equity_curve[i] - prev) / prev)
    mean = sum(rets) / len(rets) if rets else 0.0
    std = _std(rets, mean)
    downside = _std([min(r, 0.0) for r in rets], 0.0)
    af = _ann_factor(interval)

    sharpe = (mean / std * af) if std > 0 else 0.0
    sortino = (mean / downside * af) if downside > 0 else 0.0

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win and 99.9 or 0.0)
    win_rate = (len(wins) / len(trade_pnls) * 100.0) if trade_pnls else 0.0
    avg_trade = (sum(trade_pnls) / len(trade_pnls)) if trade_pnls else 0.0

    # max drawdown по эквити
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    return {
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "profit_factor": round(profit_factor, 2),
        "win_rate": round(win_rate, 1),
        "max_dd": round(max_dd * 100.0, 2),
        "avg_trade": round(avg_trade, 2),
        "total_trades": len(trade_pnls),
    }


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95%-интервал Уилсона для доли (lo, hi) в [0,1]. Перенос из Polymarket
    (strategy.wilson_ci). n=0 -> (0,1) (нет информации). Уместен для доли прибыльных
    round-trip и доли adverse-филлов — честная неопределённость на малой выборке."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def inventory_stats(inv_curve: list[float]) -> dict:
    """Поведение инвентаря во времени: гуляет вокруг нуля или накапливается направленно."""
    n = len(inv_curve)
    if n == 0:
        return {"mean": 0.0, "mean_abs": 0.0, "max_abs": 0.0, "std": 0.0,
                "share_flat": 0.0, "share_long": 0.0, "share_short": 0.0, "n": 0}
    mean = sum(inv_curve) / n
    mean_abs = sum(abs(x) for x in inv_curve) / n
    max_abs = max(abs(x) for x in inv_curve)
    var = sum((x - mean) ** 2 for x in inv_curve) / (n - 1) if n > 1 else 0.0
    flat = sum(1 for x in inv_curve if abs(x) < 1e-9)
    longs = sum(1 for x in inv_curve if x > 1e-9)
    shorts = sum(1 for x in inv_curve if x < -1e-9)
    return {"mean": round(mean, 6), "mean_abs": round(mean_abs, 6),
            "max_abs": round(max_abs, 6), "std": round(math.sqrt(var), 6),
            "share_flat": round(flat / n, 3), "share_long": round(longs / n, 3),
            "share_short": round(shorts / n, 3), "n": n}


def _std(xs: list[float], mean: float) -> float:
    if len(xs) < 2:
        return 0.0
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _empty() -> dict:
    return {"sharpe": 0.0, "sortino": 0.0, "profit_factor": 0.0, "win_rate": 0.0,
            "max_dd": 0.0, "avg_trade": 0.0, "total_trades": 0}
