# -*- coding: utf-8 -*-
"""Юнит-тесты канонического Avellaneda-Stoikov (app/strategy/avellaneda_canonical.py).
Проверяемые свойства (бриф claude_code_mm_engine.md, блок A):
  - reservation price смещается ПРОТИВ инвентаря (лонг -> r<mid, шорт -> r>mid);
  - при q=0 котировки СИММЕТРИЧНЫ вокруг mid (r==mid);
  - полный спред РАСТЁТ с sigma и с gamma;
  - дистанция котировок СОРАЗМЕРНА sigma (а не фиксированным множителям ATR);
  - fit_intensity восстанавливает kappa из синтетического экспоненциального потока.
Запуск: pytest -q  (или python test_avellaneda_canonical.py)
"""
import math

from app.strategy import avellaneda_canonical as ac

MID = 65000.0


def test_q_zero_symmetric():
    """q=0 -> reservation == mid, bid/ask симметричны вокруг mid."""
    qd = ac.quote(MID, q=0.0, gamma=0.3, sigma=0.003, kappa=1000.0, T=200)
    assert abs(qd.reservation - MID) < 1e-9
    assert abs((MID - qd.bid) - (qd.ask - MID)) < 1e-6
    assert qd.bid < MID < qd.ask
    assert qd.half_spread > 0


def test_reservation_against_inventory():
    """Лонг -> центр ниже mid; шорт -> выше; модуль смещения растёт с |q|."""
    longq = ac.quote(MID, q=1.0, gamma=0.3, sigma=0.01, kappa=1000.0, T=200)
    shortq = ac.quote(MID, q=-1.0, gamma=0.3, sigma=0.01, kappa=1000.0, T=200)
    flat = ac.quote(MID, q=0.0, gamma=0.3, sigma=0.01, kappa=1000.0, T=200)
    assert longq.reservation < MID, "лонг должен сдвигать центр НИЖЕ mid"
    assert shortq.reservation > MID, "шорт должен сдвигать центр ВЫШЕ mid"
    big = ac.quote(MID, q=2.0, gamma=0.3, sigma=0.01, kappa=1000.0, T=200)
    assert (MID - big.reservation) > (MID - longq.reservation) > (MID - flat.reservation)


def test_both_quotes_shift_down_when_long():
    """Инвентарный скос: при лонге И bid, И ask смещаются вниз (сброс инвентаря)."""
    flat = ac.quote(MID, q=0.0, gamma=0.5, sigma=0.01, kappa=1000.0, T=200)
    longq = ac.quote(MID, q=1.0, gamma=0.5, sigma=0.01, kappa=1000.0, T=200)
    assert longq.bid < flat.bid
    assert longq.ask < flat.ask


def test_spread_grows_with_sigma():
    """Полный спред монотонно растёт с волатильностью sigma."""
    s1 = ac.spread_frac(gamma=0.3, sigma=0.005, kappa=1000.0, T=200)
    s2 = ac.spread_frac(gamma=0.3, sigma=0.010, kappa=1000.0, T=200)
    s3 = ac.spread_frac(gamma=0.3, sigma=0.020, kappa=1000.0, T=200)
    assert s1 < s2 < s3


def test_spread_grows_with_gamma():
    """Полный спред растёт с риск-аверсией gamma (γ — честный регулятор)."""
    g1 = ac.spread_frac(gamma=0.1, sigma=0.01, kappa=1000.0, T=10)
    g2 = ac.spread_frac(gamma=0.5, sigma=0.01, kappa=1000.0, T=10)
    g3 = ac.spread_frac(gamma=2.0, sigma=0.01, kappa=1000.0, T=10)
    assert g1 < g2 < g3


def test_distance_scales_with_sigma():
    """Дистанция котировок СОРАЗМЕРНА sigma: удвоение sigma увеличивает half_spread
    (а не фиксированные 1.3-2.4 ATR прежней самоделки)."""
    q1 = ac.quote(MID, q=0.0, gamma=0.5, sigma=0.005, kappa=1000.0, T=200)
    q2 = ac.quote(MID, q=0.0, gamma=0.5, sigma=0.010, kappa=1000.0, T=200)
    assert q2.half_spread > q1.half_spread
    # half_spread должен быть осмысленной долей цены (единицы bps..проценты), не абсурд
    assert 1e-4 * MID < q2.half_spread < 0.1 * MID


def test_kappa_widens_when_smaller():
    """Меньшая κ (поток затухает быстрее с расстоянием) -> шире спред."""
    tight = ac.spread_frac(gamma=0.3, sigma=0.003, kappa=2000.0, T=200)
    wide = ac.spread_frac(gamma=0.3, sigma=0.003, kappa=200.0, T=200)
    assert wide > tight


def test_fit_intensity_recovers_kappa():
    """fit_intensity восстанавливает κ из синтетического lambda(δ)=e^(-κδ)."""
    k_true = 200.0
    M = 300
    distances = []
    flags = []
    for i in range(16):
        d = i * 0.0005                     # 0 .. 0.0075 (доли цены)
        n_fill = int(round(M * math.exp(-k_true * d)))
        for j in range(M):
            distances.append(d)
            flags.append(1 if j < n_fill else 0)
    res = ac.fit_intensity(distances, flags, bins=8)
    assert res is not None, "должна вернуться оценка (κ, A)"
    kappa, A = res
    assert kappa > 0
    assert 0.5 * k_true < kappa < 2.0 * k_true, f"κ={kappa:.1f} вне диапазона вокруг {k_true}"


def test_fit_intensity_insufficient_data():
    """Мало данных -> None (честно, без выдумывания κ)."""
    assert ac.fit_intensity([0.001, 0.002], [1, 0], bins=8) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (avellaneda_canonical)")
