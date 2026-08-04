"""Спецификации фьючерсов MOEX для Replay-as-Live.

Источник цен — локальные архивы (Finam-CSV, 1m) в D:\\DEFI. Каждый инструмент
описан безразмерно (point_value — рублёвая стоимость одного пункта цены за 1 контракт),
чтобы PnL считался в рублях, а параметры стратегии (k×ATR) оставались сравнимыми.

ВАЖНО про стоимость шага:
- Si (USD/RUB) и Eu (EUR/RUB): стоимость пункта ФИКСИРОВАНА = 1 ₽/пункт за 1 контракт
  (спецификация MOEX). Точно.
- ED (EUR/USD) и RTS (индекс РТС): рублёвая стоимость пункта ПЛАВАЮЩАЯ — зависит от
  текущего курса USD/RUB, которого в архиве цен нет. На первом этапе берём
  ПРИБЛИЖЁННЫЙ коэффициент (помечено approx=True). Уточняется позже (ALOR/спека).

ALOR API подключим вторым источником за тем же интерфейсом (см. DataSource ниже) —
сейчас только локальный архив.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FuturesSpec:
    code: str           # тикер архива (Si/Eu/ED/RTS)
    label: str          # человекочитаемое имя
    filename: str       # имя файла архива в D:\DEFI
    point_value: float  # ₽ за 1 пункт цены за 1 контракт
    price_step: float   # минимальный шаг цены (пункт)
    margin: float       # ориентировочное ГО (₽ за 1 контракт) — контроль позиции
    approx: bool        # True → стоимость пункта приближённая (ED/RTS)
    note: str = ""


# Приближённый курс USD/RUB для ED/RTS (стоимость пункта в ₽). Помечено approx.
# RTS: 1 пункт индекса = $0.02 → ×USDRUB(~90) ≈ 1.8 ₽/пункт. Шаг RTS = 10 пунктов.
# ED: 1 пункт = 0.0001 EUR/USD, контракт 1000 EUR → $0.10 за пункт → ×90 ≈ 9 ₽/пункт.
_USDRUB_APPROX = 90.0

SPECS: dict[str, FuturesSpec] = {
    "Si": FuturesSpec("Si", "Si · USD/RUB", "Si.txt", point_value=1.0, price_step=1.0,
                      margin=10000.0, approx=False,
                      note="Стоимость пункта 1 ₽ — фиксировано (спека MOEX)."),
    "Eu": FuturesSpec("Eu", "Eu · EUR/RUB", "Eu.txt", point_value=1.0, price_step=1.0,
                      margin=12000.0, approx=False,
                      note="Стоимость пункта 1 ₽ — фиксировано (спека MOEX)."),
    "ED": FuturesSpec("ED", "ED · EUR/USD", "ED.txt",
                      point_value=round(0.10 * _USDRUB_APPROX, 4), price_step=0.0001,
                      margin=9000.0, approx=True,
                      note=f"Стоимость пункта ПРИБЛИЖЁННО (USD/RUB≈{_USDRUB_APPROX}); уточнить."),
    "RTS": FuturesSpec("RTS", "RTS · индекс РТС", "RTS.txt",
                       point_value=round(0.02 * _USDRUB_APPROX, 4), price_step=10.0,
                       margin=20000.0, approx=True,
                       note=f"Стоимость пункта ПРИБЛИЖЁННО (USD/RUB≈{_USDRUB_APPROX}); уточнить."),
}

DEFAULT_CODE = "Si"


def data_root() -> str:
    """Папка с архивами — D:\\DEFI (рядом с gridlab)."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../gridlab
    return os.path.dirname(here)  # .../DEFI


def spec_path(code: str) -> str:
    spec = SPECS.get(code) or SPECS[DEFAULT_CODE]
    return os.path.join(data_root(), spec.filename)


def list_specs() -> list[dict]:
    out = []
    for code, s in SPECS.items():
        out.append({
            "code": code, "label": s.label, "point_value": s.point_value,
            "price_step": s.price_step, "margin": s.margin, "approx": s.approx,
            "note": s.note, "exists": os.path.exists(spec_path(code)),
        })
    return out
