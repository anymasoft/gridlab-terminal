# -*- coding: utf-8 -*-
"""Бумажная сессия не останавливается сама. Никогда.

Счёт создан для того, чтобы рисковать без потерь. Если оставить его на ночь,
утром должен быть результат — любой, хоть минус в пол-депозита. Три причины
могли раньше показать вместо результата пустой экран:

  1. ликвидация — движок помечал её терминальной и больше не котировал;
  2. отказ биржи (объём ниже минимального лота, не хватило обеспечения);
  3. kill-switch по просадке.

Ни одна из них теперь не останавливает торговлю дольше, чем на один цикл
подъёма. Единственное, что снимает сетку, — кнопка «Стоп», нажатая человеком.

Что при этом НЕ подделывается: убыток остаётся убытком, ликвидация пишется
в журнал как ликвидация, а довнесение денег после неё видно отдельной записью.

Запуск: pytest -q test_never_stops.py
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


def _candles(p0=100.0, n=200):
    out, ts = [], 1_700_000_000_000
    for i in range(n):
        p = p0 * (1.0 + 0.002 * ((i % 7) - 3))
        out.append(Candle(ts=ts + i * 900_000, o=p, h=p * 1.001,
                          l=p * 0.999, c=p, v=1000.0))
    return out


def _session(capital=1000.0):
    sess = LiveSession(S, GridParams(), "15")
    sess.capital = capital
    sess.free_cash = capital
    sess.cost = CostModel.from_settings(S)
    for sym in ("AAAUSDT", "BBBUSDT"):
        e = PaperEngine(sym, 0.0, GridParams(), sess.cost, None,
                        spec={"tick_size": 0.0, "qty_step": 0.0, "min_qty": 0.0})
        e.warm(_candles())
        sess.engines[sym] = e
        sess.hist[sym] = _candles()
    sess.state["selected"] = "AAAUSDT"
    return sess


# ─────────────── подъём на уровне движка ───────────────
def test_revive_clears_liquidation():
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.liquidated = True
    e.active = False
    why = e.revive()
    assert not e.liquidated, "ликвидация обязана сниматься"
    assert "после ликвидации" in why, "причина обязана называться, а не теряться"


def test_revive_clears_killswitch_and_exchange_refusal():
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.halted = True
    e.blocked_reason = "объём ордера ниже минимального лота (0.001)"
    e.rejected_min_qty = 40
    why = e.revive()
    assert not e.halted and not e.blocked_reason
    assert e.rejected_min_qty == 0
    assert "kill-switch" in why and "отказа биржи" in why


def test_revive_resets_drawdown_baseline():
    """Просадка после подъёма считается заново — иначе kill-switch сработает
    мгновенно от старого пика и остановка вернётся на следующем же тике."""
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.peak_equity = 10_000.0
    e.revive()
    assert abs(e.peak_equity - e.cash) < 1e-9


# ─────────────── подъём на уровне сессии ───────────────
def _liquidate(sess, sym):
    """Ликвидация как она есть: позиция закрыта, доля инструмента сгорела."""
    e = sess.engines[sym]
    e.liquidated = True
    e.active = False
    e.orders = []
    e.cash = 0.0
    return e


def test_liquidated_grid_comes_back():
    """Главный сценарий: ночью сетку ликвидировало — утром она снова торгует.

    Деньги берутся из того, что осталось на счёте: незанятое у работающих сеток
    плюс свободные. Обеспечение под их открытыми позициями не трогается."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    sess.start_strategy("BBBUSDT")
    e = _liquidate(sess, "AAAUSDT")

    assert sess.revive_stopped() == 1, "сетка не поднялась"
    assert e.active and e.orders, "торговля не возобновилась"
    assert e.cash > 0, "деньги на торговлю не выданы"
    assert not e.liquidated


def test_revival_after_liquidation_is_written_down_as_a_top_up():
    """Довнесение обязано быть видно в журнале: иначе утренняя цифра читается
    как прибыль из ниоткуда, хотя на деле сетку долили после ликвидации."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    sess.start_strategy("BBBUSDT")
    e = _liquidate(sess, "AAAUSDT")
    sess.revive_stopped()

    ev = [x for x in e.events if x.action == "Торговля возобновлена"]
    assert ev, "запись о возобновлении не сделана"
    assert "после ликвидации" in ev[-1].comment
    assert "довнесено" in ev[-1].comment


def test_empty_account_says_so_instead_of_pretending():
    """Если счёт израсходован полностью, поднимать нечем — и это тоже результат.
    Он обязан быть назван, а не выглядеть как «ничего не произошло»."""
    sess = _session()
    sess.start_strategy("AAAUSDT")          # одна сетка забрала весь счёт
    e = _liquidate(sess, "AAAUSDT")         # и сгорела вместе с ним

    assert sess.account_money() < 1e-6, "контроль: на счёте не должно остаться денег"
    assert sess.revive_stopped() == 0
    assert any(x.action == "Торговать нечем" for x in e.events),         "движок молча ничего не сделал вместо того, чтобы объяснить"


def test_blocked_by_exchange_grid_comes_back():
    sess = _session()
    sess.start_strategy("AAAUSDT")
    e = sess.engines["AAAUSDT"]
    e.active = False
    e.orders = []
    e.blocked_reason = "не хватает обеспечения под заявки"

    assert sess.revive_stopped() == 1
    assert e.active and not e.blocked_reason


def test_killswitch_does_not_stop_the_night():
    sess = _session()
    sess.start_strategy("AAAUSDT")
    e = sess.engines["AAAUSDT"]
    e.halted = True
    e.active = False
    e.orders = []

    assert sess.revive_stopped() == 1
    assert e.active and not e.halted


def test_manual_stop_is_the_only_thing_that_sticks():
    """Кнопка «Стоп» — единственное, что действительно снимает сетку.
    Иначе остановить торговлю было бы невозможно вовсе."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    sess.stop_strategy("AAAUSDT")
    assert "AAAUSDT" not in sess.wanted

    assert sess.revive_stopped() == 0, "остановленная руками сетка не должна воскресать"
    assert not sess.engines["AAAUSDT"].active


def test_revival_does_not_invent_money():
    """Подъём не создаёт денег: сумма счёта до и после совпадает, если
    доливать было нечего, и растёт ровно на взятое из свободных, если было."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    sess.start_strategy("BBBUSDT")
    e = sess.engines["AAAUSDT"]

    total_before = sess.free_cash + sum(x.cash for x in sess.engines.values())
    e.liquidated = True
    e.active = False
    e.cash = 0.0                                   # ликвидация сожгла долю
    burned = total_before - (sess.free_cash + sum(x.cash for x in sess.engines.values()))
    assert burned > 0, "контроль: ликвидация обязана уменьшить счёт"

    sess.revive_stopped()
    total_after = sess.free_cash + sum(x.cash for x in sess.engines.values())
    assert total_after <= total_before + 1e-9, "подъём создал деньги из воздуха"


def test_revival_survives_restart():
    """Список «что торговать» переживает перезапуск: иначе после падения
    сервера ночью сетки поднялись бы, но некому было бы их поднимать."""
    import tempfile
    from pathlib import Path
    sess = _session()
    sess.session_path = Path(tempfile.mkdtemp()) / "s.json"
    sess.start_strategy("AAAUSDT")
    sess._persist()

    fresh = _session()
    fresh.session_path = sess.session_path
    fresh._restore()
    assert fresh.wanted == {"AAAUSDT"}
    assert fresh.never_stop is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (торговля не останавливается)")


def test_new_account_stops_everything_for_good():
    """«Новый счёт» снимает торговлю со всех пар. Иначе автоподъём через
    полминуты вернул бы обратно всё, что работало до сброса."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    sess.start_strategy("BBBUSDT")
    assert len(sess.wanted) == 2

    sess.reset_account(1000)
    assert not sess.wanted, "после сброса счёта торговать никто не просил"
    assert sess.revive_stopped() == 0


# ─────────────── видимость потерь ───────────────
def test_liquidation_is_counted_and_the_burn_recorded():
    """Ноль на счёте после одной ликвидации и после пяти выглядит одинаково.
    Счётчик и сумма сгоревшего — единственное, что их различает."""
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.warm(_candles())
    e.active = True
    e._apply_fill("buy", 30.0, 100.0, True, 1, "")     # позиция много больше обеспечения
    e.mark_price = 90.0
    assert e._check_liquidation(2, 90.0), "контроль: тут обязана быть ликвидация"

    assert e.liq_count == 1
    assert e.liq_burned > 0, "сгоревшие деньги не записаны"
    assert e.equity() >= -1e-9, "эквити не может уйти ниже нуля при изолированной марже"


def test_revive_does_not_erase_the_history_of_losses():
    """Подъём возвращает торговлю, но не стирает память о том, что ночь была тяжёлой."""
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.liq_count, e.liq_burned = 3, 742.5
    e.liquidated = True
    e.revive()
    assert e.liq_count == 3 and e.liq_burned == 742.5


def test_liquidation_counters_survive_restart():
    e = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    e._last_price = 100.0
    e.liq_count, e.liq_burned = 4, 1273.0
    fresh = PaperEngine("T", 100.0, GridParams(), CostModel.from_settings(S), None)
    fresh.load_state(e.to_state())
    assert fresh.liq_count == 4 and fresh.liq_burned == 1273.0


def test_account_cannot_go_below_the_deposit():
    """Дно счёта — минус весь депозит, как на реальной бирже при изолированной
    марже. Показать убыток глубже внесённого значило бы соврать про реальный счёт."""
    sess = _session()
    sess.start_strategy("AAAUSDT")
    _liquidate(sess, "AAAUSDT")
    assert sess.account_money() >= -1e-9
    assert sess.account_money() - sess.capital >= -sess.capital - 1e-9
