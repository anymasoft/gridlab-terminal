"""Модель издержек — как на реальном Bybit (perpetual). Все цифры из .env,
чтобы калибровать под фактические комиссии/проскальзывание."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings


@dataclass
class CostModel:
    maker_fee: float        # доля (0.0002 = 0.02%)
    taker_fee: float
    slippage: float         # доля проскальзывания для market
    leverage: float
    maint_margin: float     # поддерживающая маржа (доля notional)
    funding_interval_ms: int
    default_funding_rate: float = 0.0001  # запасная 8h-ставка, если истории нет

    @classmethod
    def from_settings(cls, s: Settings) -> "CostModel":
        return cls(
            maker_fee=s.maker_fee_bps / 10000.0,
            taker_fee=s.taker_fee_bps / 10000.0,
            slippage=s.slippage_bps / 10000.0,
            leverage=s.leverage,
            maint_margin=s.liq_maint_margin_bps / 10000.0,
            funding_interval_ms=s.funding_interval_h * 3600 * 1000,
        )

    def fee(self, notional: float, is_maker: bool) -> float:
        return abs(notional) * (self.maker_fee if is_maker else self.taker_fee)

    def fill_price(self, price: float, side: str, is_maker: bool) -> float:
        """Maker исполняется по своей цене; market/taker — с проскальзыванием против нас."""
        if is_maker:
            return price
        return price * (1 + self.slippage) if side == "buy" else price * (1 - self.slippage)
