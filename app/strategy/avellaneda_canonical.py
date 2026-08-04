"""Канонический market-maker Avellaneda-Stoikov (2008), реализованный из первых
принципов. Заменяет самодельную ATR-нормированную эвристику в quoting._avellaneda.

Диагностика (REPORT_GRIDLAB_DIAGNOSIS.md) показала: прежний _avellaneda — НЕ A-S,
а подгонка с эмпирическими коэффициентами (0.5/0.6/0.25/0.04); дистанция заявок
выходила 1.3-2.4 ATR при ходе бара ~0.35 ATR. Здесь — честная модель.

МОДЕЛЬ
------
Reservation price (центр котирования смещается ПРОТИВ инвентаря):
    r = s - q*gamma*sigma^2*(T-t)
Оптимальный полный спред:
    delta = gamma*sigma^2*(T-t) + (2/gamma)*ln(1 + gamma/kappa)
Котировки: bid = r - delta/2,  ask = r + delta/2.

ОБОЗНАЧЕНИЯ И ЕДИНИЦЫ (выбор зафиксирован)
------------------------------------------
s     - mid-price.
sigma - волатильность ДОХОДНОСТЕЙ за один шаг (доля, безразмерная), НЕ ATR.
        Берётся из реальных данных: realized vol mid (Indicators.sigma в бэктесте;
        live - из обновлений mid реального стакана, блоки B/C).
q     - знаковый инвентарь, нормированный в БЕЗРАЗМЕРНОЕ отношение (q>0 = лонг).
gamma - риск-аверсия: главный настраиваемый регулятор риск/доходности.
kappa - интенсивность исполнения: lambda(delta)=A*e^(-kappa*delta), delta -
        расстояние котировки от mid В ДОЛЯХ цены. Оценивается из реального потока
        сделок (fit_intensity, блок C). До накопления статистики - ВРЕМЕННЫЙ
        дефолт DEFAULT_KAPPA, помеченный как временный.
(T-t) - остаток горизонта. Маркет-мейкинг НЕПРЕРЫВЕН (нет терминального времени),
        поэтому используется СТАЦИОНАРНЫЙ скользящий горизонт: (T-t) == T = const
        (окно в барах, параметр). Это стандартное упрощение бесконечного горизонта:
        член затухания времени заменяется постоянным окном обзора. Больше T -> шире
        спред и сильнее инвентарный скос (осторожнее на длинном горизонте).

ФОРМУЛИРОВКА (фракционная / в пространстве доходностей)
------------------------------------------------------
sigma задана в долях, поэтому ВСЕ члены модели вычисляются как ДОЛИ цены и затем
масштабируются множителем s в абсолютную цену. Это эквивалент A-S при
геометрической динамике цены (log-price) и даёт два практических свойства:
(1) дистанция котировок СОРАЗМЕРНА sigma (а не фиксированным множителям ATR);
(2) gamma остаётся безразмерным регулятором масштаба O(0.1..3), одинаково
    осмысленным и для BTC, и для фьючерса Si.
Это сознательный выбор непрерывной формулировки (бриф claude_code_mm_engine.md).

ЧЕСТНАЯ ОГОВОРКА
---------------
Канонический A-S делает математику корректной и даёт честный регулятор риска
(gamma), но НЕ ГАРАНТИРУЕТ ПРИБЫЛЬ. Модель предполагает (приблизительно)
случайный поток сделок и mid как мартингейл; реальный adverse selection на
трендовых инструментах может давать убыток даже при правильной математике. Это
проверяется ДАННЫМИ после установки правильной модели, а не постулируется.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Временный дефолт kappa (фракционные единицы: delta в долях цены). Уточняется из
# реального потока сделок в блоке C (fit_intensity). Помечен как ВРЕМЕННЫЙ.
DEFAULT_KAPPA: float = 1000.0


@dataclass
class ASQuote:
    """Котировки A-S для ОДНОЙ пары bid/ask (осознанно одна пара, без сетки)."""
    mid: float
    reservation: float      # r - центр, смещённый против инвентаря
    bid: float
    ask: float
    half_spread: float      # delta/2 в цене
    delta_total: float      # delta в цене
    skew: float             # s - r  (>0 при лонге: центр ниже mid)


def _clean(gamma: float, sigma: float, kappa: float, T: float) -> tuple[float, float, float, float]:
    return (max(float(gamma), 1e-9), max(float(sigma), 0.0),
            max(float(kappa), 1e-9), max(float(T), 0.0))


def reservation_offset_frac(q: float, gamma: float, sigma: float, T: float) -> float:
    """Доля смещения reservation price от mid: q*gamma*sigma^2*T.
    >0 -> центр НИЖЕ mid (при лонге q>0): осторожнее покупать, охотнее продавать."""
    g, sg, _, t = _clean(gamma, sigma, 1.0, T)
    return q * g * sg * sg * t


def spread_frac(gamma: float, sigma: float, kappa: float, T: float) -> float:
    """Оптимальный полный спред в ДОЛЯХ цены: gamma*sigma^2*T + (2/gamma)*ln(1+gamma/kappa).
    Первый член (инвентарный/волатильный) растёт с sigma, gamma, T; второй (за поток)
    задаёт базовую надбавку и при малом gamma/kappa стремится к 2/kappa."""
    g, sg, kp, t = _clean(gamma, sigma, kappa, T)
    inventory_term = g * sg * sg * t
    flow_term = (2.0 / g) * math.log1p(g / kp)
    return inventory_term + flow_term


def quote(mid: float, q: float, gamma: float, sigma: float,
          kappa: float = DEFAULT_KAPPA, T: float = 1.0) -> ASQuote:
    """Канонические котировки A-S вокруг reservation price (одна пара bid/ask)."""
    g, sg, kp, t = _clean(gamma, sigma, kappa, T)
    skew_frac = q * g * sg * sg * t
    r = mid * (1.0 - skew_frac)
    delta = mid * spread_frac(g, sg, kp, t)
    half = delta / 2.0
    return ASQuote(mid=mid, reservation=r, bid=r - half, ask=r + half,
                   half_spread=half, delta_total=delta, skew=mid - r)


def fit_intensity(distances_frac, filled_flags, bins: int = 8):
    """Оценка kappa из реального потока: lambda(delta)=A*e^(-kappa*delta).

    distances_frac - расстояния выставленных котировок от mid (в ДОЛЯХ цены);
    filled_flags   - 1/0 (исполнилась ли котировка на этом расстоянии).
    Группируем наблюдения по расстоянию в бины, по доле исполнений в каждом бине
    строим log-линейную регрессию ln(rate) = lnA - kappa*delta (МНК по бинам).
    Возвращает (kappa, A) или None при нехватке данных.

    ЭТО ОЦЕНКА (документированная граница): блок C даст реальный поток сделок;
    метод и есть «полный A-S по мере появления L2-данных»."""
    pts = [(float(d), 1.0 if f else 0.0)
           for d, f in zip(distances_frac, filled_flags)
           if d is not None and d >= 0]
    if len(pts) < bins * 2:
        return None
    pts.sort(key=lambda x: x[0])
    n = len(pts)
    per = max(1, n // bins)
    xs: list[float] = []
    ys: list[float] = []
    for i in range(0, n, per):
        chunk = pts[i:i + per]
        if len(chunk) < 2:
            continue
        d_mean = sum(d for d, _ in chunk) / len(chunk)
        rate = sum(f for _, f in chunk) / len(chunk)
        if rate <= 0:
            continue
        xs.append(d_mean)
        ys.append(math.log(rate))
    if len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    kappa = -slope            # lambda спадает с delta -> slope<0 -> kappa>0
    if kappa <= 0:
        return None
    A = math.exp(my - slope * mx)
    return kappa, A
