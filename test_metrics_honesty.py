# -*- coding: utf-8 -*-
"""Тесты честности метрик: неопределённость показывается рядом с оценкой.

Годовой Sharpe на десяти сутках легко выходит 7–8. Само по себе это число читается
как «отличная стратегия», хотя статистически неотличимо от нуля. Поэтому рядом
обязаны быть стандартная ошибка и длина выборки, а у доли прибыльных сделок —
доверительный интервал.

Запуск: pytest -q test_metrics_honesty.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.analytics import metrics as M          # noqa: E402


def _curve(n, drift=0.0005, vol=0.002, seed=3):
    rnd = random.Random(seed)
    eq, cur = [1000.0], 1000.0
    for _ in range(n):
        cur *= 1.0 + drift + rnd.gauss(0, vol)
        eq.append(cur)
    return eq


def test_sharpe_carries_standard_error_and_sample_length():
    """К годовому Sharpe обязаны прилагаться ± и длина выборки."""
    a = M.compute(_curve(1000), [1.0, -0.5, 2.0], "15")
    assert a["sharpe_se"] > 0, "стандартная ошибка Sharpe не посчитана"
    assert a["sample_days"] > 0, "длина выборки не посчитана"
    # 1000 баров по 15 минут ≈ 10.4 суток
    assert 9.0 < a["sample_days"] < 12.0, f"ожидалось ~10 суток, получено {a['sample_days']}"


def test_short_sample_error_has_theoretical_floor():
    """Ошибка не может быть меньше √(1/n) × коэффициента годового пересчёта — это её
    нижняя граница даже при нулевом Sharpe. На 1000 барах 15m это ≈ 5.9, то есть
    любой годовой Sharpe меньше шести на такой выборке неотличим от нуля."""
    n = 1000
    a = M.compute(_curve(n), [1.0], "15")
    floor = math.sqrt(35040) / math.sqrt(n - 1)
    assert a["sharpe_se"] >= floor * 0.99, \
        f"ошибка {a['sharpe_se']} ниже теоретического минимума {floor:.2f}"
    assert floor > 5.0, "контроль: на десяти сутках минимальная ошибка велика"


def test_no_edge_gives_zero_sharpe_but_large_error():
    """Ряд без всякого преимущества: доходности строго чередуются +r/−r, среднее ноль.

    Sharpe выходит нулевым, а ошибка — около шести. Это и есть смысл показывать их
    вместе: на десяти сутках любое значение годового Sharpe в пределах ±6 означает
    ровно то же самое, что и ноль, — преимущества не видно."""
    eq, cur, r = [1000.0], 1000.0, 0.002
    for i in range(1000):
        cur *= (1.0 + r) if i % 2 == 0 else (1.0 / (1.0 + r))
        eq.append(cur)
    a = M.compute(eq, [1.0], "15")
    assert abs(a["sharpe"]) < 1.0, f"на ряде без преимущества Sharpe ≈ 0, получено {a['sharpe']}"
    assert a["sharpe_se"] > 5.0, f"ошибка обязана быть велика, получено {a['sharpe_se']}"
    assert a["sharpe_se"] > abs(a["sharpe"])


def test_longer_sample_shrinks_the_error():
    """Ошибка обязана падать с ростом выборки — иначе она посчитана неверно."""
    a = M.compute(_curve(1000), [1.0], "15")
    b = M.compute(_curve(16000), [1.0], "15")
    assert b["sample_days"] > a["sample_days"] * 10
    assert b["sharpe_se"] < a["sharpe_se"], \
        f"ошибка не уменьшилась: {a['sharpe_se']} -> {b['sharpe_se']}"


def test_win_rate_has_confidence_interval():
    """Доля прибыльных сделок отдаётся с интервалом Уилсона, и он накрывает оценку."""
    a = M.compute(_curve(500), [1.0, 1.0, 1.0, -1.0], "15")
    assert a["win_rate"] == 75.0
    assert a["win_rate_lo"] < a["win_rate"] < a["win_rate_hi"]
    assert a["win_rate_hi"] - a["win_rate_lo"] > 20.0, \
        "на четырёх сделках интервал обязан быть широким"


def test_interval_narrows_with_more_trades():
    few = M.compute(_curve(500), [1.0, 1.0, 1.0, -1.0], "15")
    many = M.compute(_curve(500), [1.0, 1.0, 1.0, -1.0] * 100, "15")
    assert many["win_rate"] == few["win_rate"], "доля та же"
    assert (many["win_rate_hi"] - many["win_rate_lo"]) < (few["win_rate_hi"] - few["win_rate_lo"])


def test_metrics_declare_they_are_net_of_fees():
    """Флаг для интерфейса: в метрики идёт чистый PnL, а не валовой."""
    assert M.compute(_curve(100), [1.0], "15")["net_of_fees"] is True
    assert M._empty()["net_of_fees"] is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (честность метрик)")
