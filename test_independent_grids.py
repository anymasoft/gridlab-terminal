# -*- coding: utf-8 -*-
"""Независимые сетки: каждая пара запускается отдельно, со своей долей счёта.

Раньше «Старт стратегии» запускал все десять пар разом на общих параметрах, а
капитал был размазан risk-parity по всей корзине заранее. Из-за этого на BTC
приходилось $11 на заявку при минимальном лоте $64 — торговать им было нельзя
ни при каких настройках.

Теперь незапущенная сетка денег не занимает. Проверяется главное свойство:
деньги не появляются и не исчезают ни при каких запусках и остановках.

Запуск: pytest -q test_independent_grids.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings                   # noqa: E402
from app.engine.costmodel import CostModel            # noqa: E402
from app.engine.paper import PaperEngine              # noqa: E402
from app.engine.session import LiveSession            # noqa: E402
from app.models import Candle, GridParams             # noqa: E402

S = get_settings()
SYMS = ["BTCUSDT", "SOLUSDT", "DOGEUSDT"]
PRICES = {"BTCUSDT": 64000.0, "SOLUSDT": 150.0, "DOGEUSDT": 0.07}


def _candles(p0, n=200):
    out, ts = [], 1_700_000_000_000
    for i in range(n):
        p = p0 * (1.0 + 0.002 * ((i % 7) - 3))
        out.append(Candle(ts=ts + i * 900_000, o=p, h=p * 1.001,
                          l=p * 0.999, c=p, v=1000.0))
    return out


def _session(capital=1000.0):
    """Сессия без сети: движки собираются вручную, как это делает _build."""
    sess = LiveSession(S, GridParams(), "15")
    sess.capital = capital
    sess.free_cash = capital
    sess.cost = CostModel.from_settings(S)
    specs = {"BTCUSDT": {"tick_size": 0.1, "qty_step": 0.001, "min_qty": 0.001},
             "SOLUSDT": {"tick_size": 0.01, "qty_step": 0.1, "min_qty": 0.1},
             "DOGEUSDT": {"tick_size": 1e-05, "qty_step": 1.0, "min_qty": 1.0}}
    for sym in SYMS:
        e = PaperEngine(sym, 0.0, GridParams(), sess.cost, None, spec=specs[sym])
        e.warm(_candles(PRICES[sym]))
        sess.engines[sym] = e
        sess.hist[sym] = _candles(PRICES[sym])[-150:]
    sess.state["selected"] = SYMS[0]
    return sess


def _account_total(sess):
    """Все деньги счёта: свободные плюс лежащие в сетках."""
    return sess.free_cash + sum(e.cash for e in sess.engines.values())


def test_nothing_is_allocated_until_started():
    sess = _session()
    assert sess.free_cash == 1000.0
    for e in sess.engines.values():
        assert e.alloc == 0.0 and e.cash == 0.0, "незапущенная сетка не занимает денег"


def test_first_grid_takes_the_whole_account():
    """Одна запущенная пара получает весь счёт — ради этого всё и делалось:
    при $1000 на BTC минимальный лот перестаёт быть препятствием."""
    sess = _session()
    sess.start_strategy("BTCUSDT")
    btc = sess.engines["BTCUSDT"]
    assert btc.active
    assert abs(btc.alloc - 1000.0) < 1e-9, f"BTC получил {btc.alloc}, а не весь счёт"
    assert abs(sess.free_cash) < 1e-9
    assert not btc.blocked_reason, "при $1000 BTC обязан торговаться"
    assert btc.orders, "сетка должна быть выставлена"


def test_second_grid_takes_from_the_first_but_not_from_its_position():
    """Запуск второй пары делит счёт пополам, но забирает у первой только то,
    что не занято обеспечением под уже стоящие заявки и позицию."""
    sess = _session()
    sess.start_strategy("BTCUSDT")
    btc = sess.engines["BTCUSDT"]
    locked = btc.locked_capital()

    sess.start_strategy("SOLUSDT")
    sol = sess.engines["SOLUSDT"]
    assert sol.active and sol.alloc > 0, "вторая сетка осталась без денег"
    assert btc.cash >= locked - 1e-9, "у первой сетки забрали обеспечение под позицию"
    assert abs(_account_total(sess) - 1000.0) < 1e-6, "деньги счёта изменились"


def test_stopping_returns_money_to_free():
    sess = _session()
    sess.start_strategy("SOLUSDT")
    assert sess.free_cash < 1.0
    sess.stop_strategy("SOLUSDT")
    sol = sess.engines["SOLUSDT"]
    assert not sol.active
    assert not [o for o in sol.orders if not o.manual], "сеточные заявки не сняты"
    assert sess.free_cash > 990.0, f"вернулось только {sess.free_cash}"
    assert abs(_account_total(sess) - 1000.0) < 1e-6


def test_account_total_survives_any_sequence():
    """Главный инвариант: сколько ни запускай и ни останавливай, денег на счёте
    столько же. Ошибка здесь означала бы, что счёт врёт."""
    sess = _session()
    seq = ["BTCUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT", "SOLUSDT",
           "DOGEUSDT", "BTCUSDT", "DOGEUSDT"]
    for i, sym in enumerate(seq):
        if sess.engines[sym].active:
            sess.stop_strategy(sym)
        else:
            sess.start_strategy(sym)
        assert abs(_account_total(sess) - 1000.0) < 1e-6, \
            f"после шага {i} ({sym}) на счёте {_account_total(sess)}"
        assert sess.free_cash >= -1e-9, f"свободные ушли в минус: {sess.free_cash}"


def test_params_are_per_pair():
    """У каждой пары свои настройки: у BTC может быть шаг 0.5%, у DOGE 2%."""
    sess = _session()
    sess.apply_params_live({**GridParams().model_dump(), "grid_step_pct": 0.5}, "BTCUSDT")
    sess.apply_params_live({**GridParams().model_dump(), "grid_step_pct": 2.0}, "DOGEUSDT")
    assert sess.engines["BTCUSDT"].p.grid_step_pct == 0.5
    assert sess.engines["DOGEUSDT"].p.grid_step_pct == 2.0
    assert sess.engines["SOLUSDT"].p.grid_step_pct == GridParams().grid_step_pct, \
        "пара, которую не трогали, не должна меняться"


def test_started_flag_reflects_any_running_grid():
    sess = _session()
    assert not sess.state["started"]
    sess.start_strategy("SOLUSDT")
    assert sess.state["started"]
    sess.start_strategy("DOGEUSDT")
    sess.stop_strategy("SOLUSDT")
    assert sess.state["started"], "одна сетка ещё работает"
    sess.stop_strategy("DOGEUSDT")
    assert not sess.state["started"]


def test_stopped_grid_is_not_reported_as_blocked():
    """Выключенная пара не «не торгуется» — она просто не запущена.

    Отметка блокировки означает «биржа не приняла заявки запущенной сетки».
    Если она остаётся на выключенной паре, интерфейс врёт: девять пар из десяти
    выглядят непригодными к торговле, хотя с ними всё в порядке."""
    sess = _session()
    btc = sess.engines["BTCUSDT"]
    sess.start_strategy("BTCUSDT")
    assert not btc.blocked_reason, "при $1000 BTC торгуется"

    # искусственно ставим отметку, как если бы денег не хватило
    btc.blocked_reason = "объём ордера ниже минимального лота (0.001)"
    sess.stop_strategy("BTCUSDT")
    assert not btc.blocked_reason, "остановка обязана снимать отметку блокировки"


def test_new_account_clears_previous_exchange_rejections():
    sess = _session()
    for e in sess.engines.values():
        e.blocked_reason = "объём ордера ниже минимального лота"
        e.rejected_min_qty = 40
    sess.reset_account(1000)
    for sym, e in sess.engines.items():
        assert not e.blocked_reason, f"{sym}: отметка пережила новый счёт"
        assert e.rejected_min_qty == 0
    assert abs(_account_total(sess) - 1000.0) < 1e-6


def test_starting_without_free_money_does_not_invent_it():
    """Если весь счёт держат открытые позиции, новая сетка денег не получает —
    и не выставляется. Молча занять несуществующие деньги нельзя."""
    sess = _session()
    sess.start_strategy("BTCUSDT")
    btc = sess.engines["BTCUSDT"]
    btc.cash = btc.locked_capital()      # всё занято обеспечением
    before = _account_total(sess)

    sess.start_strategy("SOLUSDT")
    sol = sess.engines["SOLUSDT"]
    assert not sol.orders, "сетка выставлена на деньги, которых нет"
    assert abs(_account_total(sess) - before) < 1e-6


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (независимые сетки)")
