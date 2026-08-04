"""Интерфейс площадки исполнения. PaperVenue — бумажный матчинг (по умолчанию).
BybitTestnetVenue — точка расширения: тот же интерфейс, реальные ордера на
Bybit Testnet (ключи в .env). Переключение = VENUE в .env, без переписывания движка.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings


class ExecutionVenue(ABC):
    name: str = "base"

    @abstractmethod
    def supports_live(self) -> bool: ...


class PaperVenue(ExecutionVenue):
    """Логическая метка площадки. Матчинг выполняет PaperEngine на реальных свечах."""
    name = "paper"

    def supports_live(self) -> bool:
        return False


class BybitTestnetVenue(ExecutionVenue):
    """Каркас под реальное исполнение на Bybit Testnet.

    Реализация (POST /v5/order/create с подписью HMAC, /v5/position/list для сверки)
    подключается здесь же — интерфейс и остальной движок не меняются. Сейчас
    требует ключи; без них фабрика честно вернёт PaperVenue.
    """
    name = "testnet"

    def __init__(self, settings: Settings):
        self.key = settings.bybit_testnet_api_key
        self.secret = settings.bybit_testnet_api_secret
        self.base = settings.bybit_testnet_url

    def supports_live(self) -> bool:
        return bool(self.key and self.secret)

    # place_order / cancel_order / fetch_position — реализуются при включении testnet


def get_venue(settings: Settings) -> ExecutionVenue:
    if settings.resolve_venue() == "testnet":
        return BybitTestnetVenue(settings)
    return PaperVenue()
