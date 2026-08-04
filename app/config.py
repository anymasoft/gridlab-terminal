"""Конфигурация GRIDLAB из .env. Площадка (paper/testnet) и модель издержек —
всё параметризовано, чтобы цифрам можно было доверять и легко калибровать под Bybit.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    venue: str = "paper"  # paper | testnet

    bybit_base_url: str = "https://api.bybit.com"
    bybit_testnet_url: str = "https://api-testnet.bybit.com"
    bybit_category: str = "linear"
    bybit_testnet_api_key: str = ""
    bybit_testnet_api_secret: str = ""

    maker_fee_bps: float = 1.0     # Bybit деривативы maker 0.01%
    taker_fee_bps: float = 6.0     # Bybit деривативы taker 0.06%
    slippage_bps: float = 1.0
    latency_ms: int = 250
    leverage: float = 3.0
    liq_maint_margin_bps: float = 50.0
    funding_interval_h: int = 8

    start_capital: float = 1000.0
    default_symbols: str = ("BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,"
                            "DOGEUSDT,AVAXUSDT,LINKUSDT,POLUSDT,ARBUSDT")
    default_interval: str = "15"
    history_bars: int = 1000

    db_path: str = "data/gridlab.db"

    # ---- производные ----
    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.default_symbols.split(",") if s.strip()]

    @property
    def active_base_url(self) -> str:
        return self.bybit_testnet_url if self.venue == "testnet" else self.bybit_base_url

    @property
    def has_testnet_keys(self) -> bool:
        return bool(self.bybit_testnet_api_key and self.bybit_testnet_api_secret)

    def resolve_venue(self) -> str:
        """Реальная площадка с учётом наличия ключей (честно деградируем в paper)."""
        if self.venue == "testnet" and self.has_testnet_keys:
            return "testnet"
        return "paper"


@lru_cache
def get_settings() -> Settings:
    return Settings()
