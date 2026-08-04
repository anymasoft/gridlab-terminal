"""Постоянная серверная сессия live-трейдинга — как биржевой аккаунт.

Один набор движков на корзину живёт в процессе НЕПРЕРЫВНО, независимо от того,
открыт браузер или нет. Состояние (счёт, позиции, ордера, сетка) сохраняется на
диск (JSON) и восстанавливается при старте: F5 и перезапуск сервера не теряют ордера.
Клиентские ws-подключения — тонкие подписчики: получают кадры и шлют действия.

Поведение приближено к Bybit: ордера лежат «на бирже», исполняются по живым тикам
даже без подключённого терминала; перезагрузка страницы показывает текущий аккаунт.
Отличие — только бумажный счёт (без реальных денег/ключей/транзакций).
"""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import os
import time
from pathlib import Path

from ..config import Settings
from ..marketdata import bybit
from ..marketdata.orderbook import OrderBookFeed
from ..marketdata.archive import ArchiveWriter
from ..models import Candle, GridParams, Position
from ..portfolio.manager import _action_color, _fmt_price
from ..strategy.indicators import Indicators
from ..analytics import metrics as _M
from .costmodel import CostModel
from .paper import PaperEngine, StepEvent
from .queue_fill import book_queue_ahead

_INTERVAL_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "30": 1_800_000,
                "60": 3_600_000, "240": 14_400_000}


def _downsample(xs, n=22):
    if len(xs) <= n:
        return list(xs)
    step = len(xs) / n
    return [xs[min(int(i * step), len(xs) - 1)] for i in range(n)]


# кривая эквити/баланса хранится на ВСЮ сессию (старт-депозит закреплён слева);
# при переполнении прорежаем вдвое, СОХРАНЯЯ индекс 0 (точку старта), а не отрезаем начало.
_CURVE_CAP = 4000
# Если по символу дольше этого не было ни одной реальной сделки, считаем поток
# замолчавшим и возвращаем движок на тиковое исполнение — лучше приблизительно,
# чем не торговать вовсе.
_TRADE_STALE_S = 20.0

# сколько последних событий журнала и маркеров филлов слать в кадре (для отображения).
# ПОЛНАЯ история сессии (без обрезки) доступна через GET /api/live/export.
_LIVE_LOG_CAP = 600


def _downsample_curve(xs, n=400):
    """Прорядить кривую до ~n точек, ГАРАНТИРУЯ первую (старт депозита) и последнюю (сейчас)."""
    if len(xs) <= n:
        return [round(v, 2) for v in xs]
    step = (len(xs) - 1) / (n - 1)
    out = [round(xs[min(int(round(i * step)), len(xs) - 1)], 2) for i in range(n)]
    out[0] = round(xs[0], 2)
    out[-1] = round(xs[-1], 2)
    return out


class LiveSession:
    def __init__(self, settings: Settings, params: GridParams, interval: str):
        self.s = settings
        self.params = params
        self.capital = float(settings.start_capital)   # текущий размер счёта (можно менять)
        self.interval = str(interval)
        self.interval_ms = _INTERVAL_MS.get(self.interval, 900_000)
        self.cost = CostModel.from_settings(settings)
        self.engines: dict[str, PaperEngine] = {}
        self.hist: dict[str, list[Candle]] = {}
        self.fmap: dict[str, dict] = {}
        self.specs: dict[str, dict] = {}        # tickSize / qtyStep / minOrderQty по символам
        self._trade_seen: dict[str, float] = {}  # когда последний раз пришла реальная сделка
        self.state = {"selected": None, "started": False}
        # Сетки независимы: у каждой пары свои параметры и своя доля капитала.
        # Общий у них только счёт — деньги, не выданные ни одной сетке, лежат
        # в free_cash и достаются той, которую запустят следующей.
        self.sym_params: dict[str, GridParams] = {}
        self.free_cash = float(settings.start_capital)
        # Что человек велел торговать. Сетка выключается сама по трём причинам —
        # ликвидация, отказ биржи, kill-switch, — и на бумажном счёте ни одна
        # из них не повод показать утром пустой экран. Пока символ здесь,
        # сессия поднимает его обратно; убирает его только кнопка «Стоп».
        self.wanted: set[str] = set()
        self.never_stop = True
        self.ob_state = {"sym": None, "data": None}
        self.ob_feed: OrderBookFeed | None = None   # живой L2-стакан по WS (блок B)
        self.archiver: ArchiveWriter | None = None  # фоновый архив L2+сделок (блок E)
        self.port_curve: list[float] = []
        self.bal_curve: list[float] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._last_broadcast = 0.0
        self.session_path = Path(settings.db_path).with_name("gridlab_session.json")

    # ───────── жизненный цикл ─────────
    async def ensure_running(self):
        if self.running:
            return
        self.running = True
        await self._build()
        self._tasks = [asyncio.create_task(self._tick_loop()),
                       asyncio.create_task(self._trades_loop()),
                       asyncio.create_task(self._ob_loop()),
                       asyncio.create_task(self._persist_loop()),
                       asyncio.create_task(self._revive_loop())]

    async def _build(self):
        # восстановить сохранённый ТФ ДО загрузки свечей (чтобы прогрев/ATR были на нужном ТФ)
        with contextlib.suppress(Exception):
            if self.session_path.exists():
                saved = json.loads(self.session_path.read_text("utf-8")).get("interval")
                if saved:
                    self.interval = str(saved)
                    self.interval_ms = _INTERVAL_MS.get(self.interval, self.interval_ms)
        cmap = await bybit.fetch_many_klines(self.s.symbols, self.interval, self.s.history_bars)
        cmap = {k: v for k, v in cmap.items() if v}
        syms = list(cmap.keys())
        for sym in syms:
            if sym not in self.fmap:
                self.fmap[sym] = await bybit.fetch_funding(sym)
        # Шаг цены, шаг лота и минимальный объём — чтобы бумажный счёт не выставлял
        # заявок, которых биржа бы не приняла.
        self.specs = await bybit.fetch_many_meta(syms, self.s)
        # Капитал НЕ размазывается по корзине заранее: незапущенная сетка денег
        # не занимает. Долю она получает в момент запуска — поэтому одна пара
        # может взять весь счёт, и минимальный лот перестаёт быть препятствием.
        self.free_cash = self.capital
        for sym in syms:
            e = PaperEngine(sym, 0.0, self.params, self.cost, self.fmap.get(sym),
                            spec=self.specs.get(sym))
            e.warm(cmap[sym])
            e.tf = self.interval
            e.last_funding_ts = cmap[sym][-1].ts
            self.engines[sym] = e
            self.hist[sym] = list(cmap[sym][-150:])
        self.state["selected"] = syms[0] if syms else None
        self._restore()

    async def set_interval(self, interval: str):
        """Сменить ТФ: перегрузить свечи/индикаторы, СОХРАНИВ счёт/позиции/ордера."""
        interval = str(interval)
        if interval == self.interval or not self.engines:
            return
        self.interval = interval
        self.interval_ms = _INTERVAL_MS.get(interval, 900_000)
        cmap = await bybit.fetch_many_klines(list(self.engines.keys()), interval, self.s.history_bars)
        for sym, e in self.engines.items():
            cs = cmap.get(sym)
            if not cs:
                continue
            e.ind = Indicators(atr_window=e.p.atr_window, ema_window=e.p.ema)
            e.warm(cs)
            e.tf = interval
            e.last_funding_ts = cs[-1].ts
            self.hist[sym] = list(cs[-150:])

    def apply_params(self, params: GridParams):
        """Применить новые параметры стратегии к живым движкам."""
        self.params = params
        for e in self.engines.values():
            e.p = params
        self._persist()

    def reset_account(self, capital):
        """Новый счёт на заданную сумму: сбросить позиции/ордера/историю, перераспределить капитал."""
        cap = float(capital)
        if not (cap > 0) or not self.engines:
            return
        self.capital = cap
        self.free_cash = cap        # весь счёт свободен: сетки берут долю при запуске
        # Новый счёт снимает торговлю со ВСЕХ пар. Без этой строки автоподъём
        # через полминуты запустил бы обратно всё, что работало до сброса.
        self.wanted.clear()
        for sym, e in self.engines.items():
            e.alloc = 0.0
            e.cash = 0.0
            e.pos = Position()
            e.orders = []
            e.fills = []
            e.events = []
            e.realized_pnls = []
            e.equity_curve_live = []
            e.trades = 0
            e.liquidated = False
            e.active = False
            e.blocked_reason = ""     # новый счёт — прежние биржевые отказы неактуальны
            e.rejected_min_qty = 0
            e.rejected_margin = 0
            e.ladder.installed = False
            e._uid = 0
            e.reset_mm()
            e.halted = False
            e.peak_equity = e.alloc
        self.state["started"] = False
        self.port_curve = [round(self.capital, 2)]   # старт депозита закреплён слева (левая точка графика)
        self.bal_curve = [round(self.capital, 2)]
        self._persist()

    # ───────── сохранение/восстановление ─────────
    def _restore(self):
        try:
            if not self.session_path.exists():
                return
            data = json.loads(self.session_path.read_text("utf-8"))
        except Exception:
            return
        self.state["started"] = bool(data.get("started"))
        if data.get("capital"):
            self.capital = float(data["capital"])
        if data.get("params"):
            with contextlib.suppress(Exception):
                self.params = GridParams(**data["params"])
                for e in self.engines.values():
                    e.p = self.params
        sel = data.get("selected")
        if sel in self.engines:
            self.state["selected"] = sel
        for sym, sp in (data.get("sym_params") or {}).items():
            with contextlib.suppress(Exception):
                self.sym_params[sym] = GridParams(**sp)
        for sym, st in (data.get("engines") or {}).items():
            e = self.engines.get(sym)
            if e:
                with contextlib.suppress(Exception):
                    e.load_state(st)
                    if "alloc" in st:
                        e.alloc = float(st["alloc"])
                    else:
                        # Состояние старого формата: alloc в нём не хранился, он
                        # выводился из risk-parity по всей корзине. Восстанавливаем
                        # его из инварианта кассы, иначе PnL сетки поедет на всю
                        # выданную ей сумму.
                        e.alloc = (e.cash - e.pos.realized
                                   + e.pos.fees_paid + e.pos.funding_paid)
                e.p = self.params_for(sym)

        self.wanted = {s for s in (data.get("wanted") or []) if s in self.engines}
        if not self.wanted:
            # состояние старого формата: считаем желанными все, кто был активен
            self.wanted = {s for s, e in self.engines.items() if e.active}
        self.never_stop = bool(data.get("never_stop", True))
        if "free_cash" in data:
            self.free_cash = float(data["free_cash"])
        else:
            # Переход с общей risk-parity раскладки на независимые сетки: деньги
            # незапущенных пар без позиций возвращаются в свободные. Суммарный
            # баланс от этого не меняется — меняется только то, где он лежит.
            self.free_cash = 0.0
            for e in self.engines.values():
                if not e.active and abs(e.pos.qty) < 1e-12 and not e.orders:
                    self.free_cash += e.cash
                    e.cash = 0.0
                    e.alloc = 0.0
        self._sync_started()
        self.port_curve = list(data.get("port_curve") or [])
        self.bal_curve = list(data.get("bal_curve") or [])

    def _persist(self):
        data = {"interval": self.interval, "started": self.state["started"],
                "capital": self.capital, "selected": self.state["selected"],
                "params": self.params.model_dump(),
                "free_cash": self.free_cash,
                "sym_params": {s: p.model_dump() for s, p in self.sym_params.items()},
            "wanted": sorted(self.wanted), "never_stop": self.never_stop,
                "engines": {sym: e.to_state() for sym, e in self.engines.items()},
                "port_curve": self.port_curve, "bal_curve": self.bal_curve}   # хранить всю сессию (≤ _CURVE_CAP)
        try:
            tmp = str(self.session_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.session_path)
        except Exception:
            pass

    # ───────── действия клиента ─────────
    def select(self, sym):
        if sym in self.engines:
            self.state["selected"] = sym

    def place_order(self, sym, side, price, qty):
        e = self.engines.get(sym)
        if e and price and qty:
            e.add_manual_order(side or "buy", float(price), float(qty))
            self._persist()

    def cancel_order(self, sym, oid):
        e = self.engines.get(sym)
        if e and oid is not None:
            e.cancel_manual(int(oid))
            self._persist()

    # ───────── независимые сетки: капитал ─────────
    def account_money(self) -> float:
        """Сколько денег на счёте СЕЙЧАС: свободные плюс лежащие в сетках.

        Доли считаются от этой величины, а не от изначального депозита: после
        убытков делить нужно то, что осталось, иначе сетке пообещают денег,
        которых на счёте уже нет."""
        return self.free_cash + sum(e.cash for e in self.engines.values())

    def _reclaim_spare(self, target: float) -> None:
        """Забрать у работающих сеток то, что превышает целевую долю И не занято
        обеспечением. Деньги под открытой позицией не трогаются — иначе позиция
        осталась бы без покрытия."""
        for x in self.engines.values():
            if not x.active:
                continue
            spare = min(x.alloc - target, x.cash - x.locked_capital())
            if spare > 1e-9:
                x.alloc -= spare
                x.cash -= spare
                self.free_cash += spare

    def start_strategy(self, sym: str | None = None):
        """Запустить сетку на ОДНОМ инструменте. Сетки независимы: у каждой свои
        параметры и своя доля капитала, общий только счёт."""
        sym = sym or self.state["selected"]
        e = self.engines.get(sym)
        if e is None or e.active or e._last_price <= 0:
            return
        if e.liquidated:
            return

        running = sum(1 for x in self.engines.values() if x.active)
        target = self.account_money() / (running + 1)
        self._reclaim_spare(target)

        need = max(0.0, target - e.alloc)
        give = min(self.free_cash, need)
        if give > 0:
            e.alloc += give           # alloc и cash растут вместе: это довнесение,
            e.cash += give            # а не прибыль — PnL от него не смещается
            self.free_cash -= give

        if e.alloc <= 0:
            e.events.append(StepEvent(
                0, "Нет свободных денег", e._last_price, 0, 0, 0,
                "весь капитал занят другими сетками — остановите одну из них"))
            self._persist()
            return

        e.p = self.params_for(sym)
        e.revive()                    # новая попытка с новой долей капитала
        e.start_strategy(e._last_price)
        self.wanted.add(sym)
        self._sync_started()
        self._persist()

    def stop_strategy(self, sym: str | None = None):
        """Остановить одну сетку и вернуть в свободные всё, что не держит позицию."""
        sym = sym or self.state["selected"]
        e = self.engines.get(sym)
        if e is None:
            return
        self.wanted.discard(sym)      # явная кнопка «Стоп» — единственное, что снимает торговлю
        e.stop_strategy()
        # Остановленная сетка не «заблокирована биржей» — она просто выключена.
        # Причину надо снять, иначе пара навсегда останется с отметкой
        # «не торгуется», даже когда ей дадут достаточно денег.
        e.blocked_reason = ""
        back = max(0.0, e.cash - e.locked_capital())
        if back > 1e-9:
            e.cash -= back
            e.alloc = max(0.0, e.alloc - back)
            self.free_cash += back
        self._sync_started()
        self._persist()

    def stop_all(self):
        for sym in list(self.engines):
            self.stop_strategy(sym)

    def _sync_started(self):
        """«Сессия торгует» = работает хотя бы одна сетка."""
        self.state["started"] = any(x.active for x in self.engines.values())

    def params_for(self, sym: str) -> GridParams:
        """Параметры конкретной сетки. Пока их не меняли — общий шаблон."""
        return self.sym_params.get(sym) or self.params

    def apply_params_live(self, params: dict, sym: str | None = None):
        """Сохранить параметры ОДНОЙ сетки и сразу переставить её уровни.

        Параметры принадлежат паре, а не сессии: у BTC может быть шаг 0.5%,
        у DOGE — 2%. Это «Сохранить», не «Запуск»."""
        try:
            gp = GridParams(**params)
        except Exception:
            return
        sym = sym or self.state["selected"]
        e = self.engines.get(sym)
        if e is None:
            return
        # Плечо обрезаем пределом БИРЖИ по этому инструменту. Позволить на бумаге
        # 100× там, где Bybit даёт 50×, значит занизить показанный риск вдвое.
        if e.max_leverage > 0 and gp.leverage > e.max_leverage:
            gp = gp.model_copy(update={"leverage": e.max_leverage})
            e.events.append(StepEvent(
                0, "Плечо ограничено", e._last_price, 0, 0, 0,
                f"биржа даёт по {sym} не больше {e.max_leverage:g}×"))
        self.sym_params[sym] = gp
        self.params = gp            # шаблон для пар, которым параметры ещё не задавали
        e.p = gp
        if e.active and not e.liquidated and e._last_price > 0:
            e.ladder.installed = False          # шаг мог измениться — лестницу ставим заново
            e._install_grid(e._last_price, 0)
        self._persist()

    def handle(self, msg: dict):
        act = msg.get("action")
        if act == "select":
            self.select(msg.get("symbol"))
        elif act == "place_order":
            self.place_order(msg.get("symbol"), msg.get("side"), msg.get("price"), msg.get("qty"))
        elif act == "cancel_order":
            self.cancel_order(msg.get("symbol"), msg.get("oid"))
        elif act == "apply_params":
            self.apply_params_live(msg.get("params") or {}, msg.get("symbol"))
        elif act == "start":
            self.start_strategy(msg.get("symbol"))
        elif act == "stop_strategy":
            self.stop_strategy(msg.get("symbol"))
        elif act == "stop_all":
            self.stop_all()
        elif act == "reset_account":
            self.reset_account(msg.get("capital"))
        # action "stop" игнорируем: сессия работает всегда, как биржа

    # ───────── фоновые задачи ─────────
    async def _tick_loop(self):
        while self.running:
            try:
                async for sym, price, mark, tts in bybit.stream_tickers(
                        list(self.engines.keys()), self.s):
                    if not self.running:
                        break
                    e = self.engines.get(sym)
                    if not e:
                        continue
                    if mark > 0:
                        e.mark_price = mark      # ликвидация считается от неё, не от last
                    cs = self.hist.get(sym)
                    if not cs:
                        continue
                    cur = cs[-1]
                    if tts - cur.ts >= self.interval_ms:
                        e.ind.update(cur)
                        cs.append(Candle(ts=cur.ts + self.interval_ms, o=price, h=price, l=price, c=price, v=0))
                        if len(cs) > 150:
                            cs.pop(0)
                        cur = cs[-1]
                    else:
                        cur.c = price
                        cur.h = max(cur.h, price)
                        cur.l = min(cur.l, price)
                    e.on_tick(price, tts)
                    e.equity_curve_live.append(e.equity())
                    if len(e.equity_curve_live) > 400:
                        e.equity_curve_live.pop(0)

                    now = time.monotonic()
                    if now - self._last_broadcast >= 0.33:
                        self._last_broadcast = now
                        # Свободные деньги — часть счёта. Без них кривая эквити
                        # проваливается в ноль, как только все сетки остановлены,
                        # и просадка показывает -100% на ровном месте.
                        total_eq = self.free_cash + sum(en.equity() for en in self.engines.values())
                        total_bal = self.free_cash + sum(en.cash for en in self.engines.values())
                        self.port_curve.append(round(total_eq, 2))
                        self.bal_curve.append(round(total_bal, 2))
                        if len(self.port_curve) > _CURVE_CAP:
                            self.port_curve = self.port_curve[::2]   # прорежаем, СТАРТ (индекс 0) сохраняется
                            self.bal_curve = self.bal_curve[::2]
                        self._broadcast(self.build_frame())
            except Exception:
                await asyncio.sleep(2)   # реконнект к Bybit ws

    async def _ob_loop(self):
        """Живой L2-стакан по WS (snapshot+дельты) для выбранного символа + поток сделок.
        Книгу/ленту держит OrderBookFeed (реконнект внутри). Пока WS-книга не готова или
        невалидна — сидируем/фолбэчим одним REST-снимком, чтобы стакан не пустовал.
        Поведение торговли (_tick_loop) не затрагивается."""
        self.ob_feed = OrderBookFeed(self.s, depth=50)
        self.ob_feed.on_trade(self._on_real_trade)   # честный trade-through по выбранному символу (блок D)
        # блок E: фоновый архив L2-стакана и ленты сделок (по выбранному символу), не мешает живой бумаге
        self.archiver = ArchiveWriter(Path(self.s.db_path).with_name("archive"))
        self.ob_feed.on_trade(lambda tr: self.archiver.record_trade(self.ob_feed.symbol, tr))
        self.ob_feed.on_book(lambda m: self.archiver.record_book(self.ob_feed.symbol, m))
        feed_task = asyncio.create_task(self.ob_feed.run(lambda: self.state["selected"]))
        arch_task = asyncio.create_task(self.archiver.run())
        try:
            while self.running:
                sym = self.state["selected"]
                if sym:
                    valid = self.ob_feed.symbol == sym and self.ob_feed.book.is_valid()
                    if valid:
                        self.ob_state["data"] = self.ob_feed.snapshot(16)   # живой WS-стакан
                        self.ob_state["sym"] = sym
                    else:
                        try:    # сид/фолбэк, пока WS-книга прогревается
                            self.ob_state["data"] = await bybit.fetch_orderbook(sym, 16, self.s)
                            self.ob_state["sym"] = sym
                        except Exception:
                            pass
                    # Исполнение по реальным сделкам ведёт _trades_loop — по ВСЕЙ корзине.
                    # Здесь только оценка позиции в очереди для выбранного символа:
                    # она требует L2-книги, а держать десять книг сразу слишком дорого.
                    self._refresh_trade_driven()
                    se = self.engines.get(sym)
                    if se is not None:
                        se.ob_book = self.ob_feed.snapshot(50) if valid else None
                await asyncio.sleep(0.5)
        finally:
            self.ob_feed.stop()
            self.archiver.stop()
            feed_task.cancel()
            arch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await feed_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await arch_task
            with contextlib.suppress(Exception):
                await self.archiver.flush()   # финальный сброс буфера на диск

    def _on_real_trade(self, tr):
        """Реальная сделка из живого стакана (блок B) -> честный trade-through матчинг
        выбранного движка (блоки C/D). Маркировку цены/эквити ведёт тикер (_tick_loop),
        фактические филлы — здесь, по реальному потоку сделок. Не трогает другие движки."""
        e = self.engines.get(self.state["selected"])
        if not e or not e.trade_driven or e.liquidated:
            return
        try:
            e.process_trade(tr.price, tr.size, tr.side, tr.ts)
        except Exception:
            pass

    async def _trades_loop(self):
        """Лента РЕАЛЬНЫХ сделок по ВСЕЙ корзине (publicTrade.<sym>).

        Зачем. Раньше честный trade-through был только у ВЫБРАННОГО инструмента, а
        остальные девять исполнялись по тикеру: цена прошла уровень — значит филл,
        без учёта реально прошедшего объёма. Это ровно тот оптимизм, из-за которого
        бумажный счёт расходится с биржей. Теперь по всей корзине заявка исполняется
        только на объём фактических сделок; оценка позиции в очереди (L2-книга) —
        по-прежнему у выбранного символа, она слишком тяжёлая для десяти сразу.

        Если поток по символу замолчал дольше _TRADE_STALE_S, движок возвращается на
        тиковое исполнение: лучше приблизительно, чем не торговать вовсе."""
        import json as _json
        syms = list(self.engines.keys())
        topics = [f"publicTrade.{s}" for s in syms]
        while self.running:
            try:
                async for msg in bybit.stream_public_ws(topics, self.s):
                    if not self.running:
                        break
                    if not str(msg.get("topic", "")).startswith("publicTrade."):
                        continue
                    for row in (msg.get("data") or []):
                        sym = row.get("s")
                        e = self.engines.get(sym)
                        if not e:
                            continue
                        try:
                            price = float(row["p"])
                            size = float(row["v"])
                            side = row.get("S") or "Buy"
                            ts = int(row.get("T") or bybit.now_ms())
                        except (KeyError, TypeError, ValueError):
                            continue
                        self._trade_seen[sym] = time.monotonic()
                        if e.liquidated or not e.active:
                            continue
                        try:
                            e.process_trade(price, size, side, ts)
                        except Exception:
                            pass
            except Exception:
                await asyncio.sleep(2)          # реконнект к Bybit ws

    def _refresh_trade_driven(self):
        """Кто исполняется по реальным сделкам, а кто откатился на тики."""
        now = time.monotonic()
        sel = self.state["selected"]
        for sym, e in self.engines.items():
            fresh = (now - self._trade_seen.get(sym, 0.0)) <= _TRADE_STALE_S
            e.trade_driven = bool(fresh and e.active)
            # L2-книга (позиция в очереди) — только у выбранного: держать десять
            # книг одновременно дорого, а без книги очередь просто не оценивается.
            if sym != sel:
                e.ob_book = None

    def revive_stopped(self) -> int:
        """Поднять все сетки, которые встали сами. Возвращает число поднятых.

        Три причины остановки — ликвидация, отказ биржи по минимальному лоту или
        обеспечению, kill-switch по просадке. На бумажном счёте ни одна из них не
        должна означать «утром смотреть не на что»: результат может быть каким
        угодно, но он должен быть.

        Что при этом НЕ подделывается: убыток остаётся убытком, ликвидация пишется
        в журнал как ликвидация, обнулённая касса не восстанавливается сама.
        Если после ликвидации у сетки не осталось денег, ей выдаётся доля
        из свободных — и это тоже пишется в журнал, чтобы утренняя цифра читалась
        правильно, а не выглядела как прибыль из ниоткуда."""
        revived = 0
        for sym in sorted(self.wanted):
            e = self.engines.get(sym)
            if e is None or e.active or e._last_price <= 0:
                continue
            why = e.revive()

            # Ликвидация обнуляет кассу. Без денег торговать нечем — берём долю
            # так же, как при обычном запуске: сгоревшая аллокация списывается,
            # у работающих сеток забирается НЕЗАНЯТОЕ (обеспечение под открытыми
            # позициями не трогается), остаток добирается из свободных.
            added = 0.0
            if e.cash <= 1e-9:
                e.alloc = 0.0                  # прежняя доля сгорела вместе с позицией
                running = sum(1 for x in self.engines.values() if x.active)
                target = self.account_money() / max(1, running + 1)
                self._reclaim_spare(target)
                added = min(self.free_cash, target)
                e.alloc += added
                e.cash += added
                self.free_cash -= added
            if e.cash <= 1e-9:
                # Денег не осталось нигде: счёт исчерпан. Это тоже результат,
                # и он должен быть виден, а не выглядеть как «ничего не произошло».
                if not any(x.action == "Торговать нечем" for x in e.events[-3:]):
                    e.events.append(StepEvent(0, "Торговать нечем", e._last_price,
                                              0, 0, 0,
                                              "весь счёт израсходован, свободных денег нет"))
                continue

            e.start_strategy(e._last_price)
            if not e.active:                   # биржа снова отказала — попробуем позже
                continue
            revived += 1
            note = why or "остановка снята"
            if added > 0:
                note += f", довнесено ${added:.2f} из свободных"
            e.events.append(StepEvent(0, "Торговля возобновлена", e._last_price,
                                      0, 0, 0, note))
        if revived:
            self._sync_started()
            self._persist()
        return revived

    async def _revive_loop(self):
        """Раз в полминуты поднимать всё, что встало. Реже нельзя — ночь длинная;
        чаще незачем — попытка стоит установки лестницы."""
        while self.running:
            await asyncio.sleep(30)
            if not self.never_stop:
                continue
            try:
                self.revive_stopped()
            except Exception:
                pass

    async def _persist_loop(self):
        while self.running:
            await asyncio.sleep(5)
            self._persist()

    def _broadcast(self, frame):
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                with contextlib.suppress(Exception):
                    q.get_nowait()
                    q.put_nowait(frame)

    # ───────── кадр ─────────
    def build_frame(self):
        engines = self.engines
        if not engines:
            return {"type": "frame", "live": True, "selected": None}
        # Свободные деньги — часть счёта: они не выданы ни одной сетке, но лежат
        # на нём. Без этого слагаемого остановка сетки выглядела бы как потеря денег.
        total_eq = self.free_cash + sum(e.equity() for e in engines.values())
        total_real = sum(e.pos.realized for e in engines.values())
        total_unreal = sum(e.pos.unrealized(e._last_price) for e in engines.values())
        total_fees = sum(e.pos.fees_paid for e in engines.values())
        total_fund = sum(e.pos.funding_paid for e in engines.values())
        open_ord = sum(len(e.orders) for e in engines.values())
        balance = self.free_cash + sum(e.cash for e in engines.values())
        roi = (total_eq - self.capital) / self.capital * 100.0 if self.capital else 0.0
        peak, dd = (self.port_curve[0] if self.port_curve else total_eq), 0.0
        for v in self.port_curve:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak)

        instruments = []
        for sym, e in engines.items():
            sm = e.summary()
            instruments.append({
                "sym": sym, "alloc": round(e.alloc, 2),
                "pnl": round(sm["pnl"], 2), "pnl_pct": round(sm["pnl_pct"], 2),
                "orders": len(e.orders),
                # Заблокированный биржевыми ограничениями инструмент обязан
                # доезжать до интерфейса и в ЖИВОЙ сессии, а не только в бэктесте:
                # иначе человек видит инструмент без сетки и без объяснения.
                # «Заблокирован» — состояние ЗАПУЩЕННОЙ сетки, которую не приняла
                # биржа. У выключенной пары такой отметки быть не должно: она не
                # «не торгуется», она просто не запущена.
                "status": ("stop" if e.liquidated
                           else "blocked" if (e.blocked_reason and not e.orders
                                              and e.alloc > 0)
                           else "active" if e.active else "idle"),
                "started": e.active,
                "blocked": e.blocked_reason,
                "min_order_usd": sm["min_order_usd"],
                "trades": e.trades,
                "spark": _downsample(e.equity_curve_live[-120:] or [e.alloc, e.equity()]),
            })

        sel = self.state["selected"]
        se = engines[sel]
        manual = [{"oid": o.oid, "side": o.side, "price": o.price, "size": o.size,
                   "queue_ahead": round(o.queue_ahead, 6)}
                  for o in se.orders if o.manual]
        grid = [{"side": o.side, "price": o.price, "size": o.size, "level": o.level,
                 "queue_ahead": round(o.queue_ahead, 6)}
                for o in se.orders if not o.manual]
        # превью следующих уровней сетки (на шаг за текущими) — только при запущенной стратегии
        preview = []
        if se.active and not se.liquidated and se._last_price > 0:
            step = se._grid_step(se._last_price)
            gbuys = [o.price for o in se.orders if not o.manual and o.side == "buy"]
            gsells = [o.price for o in se.orders if not o.manual and o.side == "sell"]
            # ровно по одной линии превью с каждой стороны — следующий уровень за активным
            if gbuys:
                preview.append({"side": "buy", "price": min(gbuys) - step})
            if gsells:
                preview.append({"side": "sell", "price": max(gsells) + step})
        ob = self.ob_state["data"] if self.ob_state["sym"] == sel else None
        tail = self.hist[sel]
        ts_arr = [c.ts for c in tail]
        markers = []
        for f in se.fills:
            if not f.is_maker:
                continue
            j = bisect.bisect_left(ts_arr, f.ts)
            j = min(max(j, 0), len(tail) - 1)
            markers.append({"i": j, "side": f.side, "price": f.price})

        raw = []
        for sym, e in engines.items():
            for ev in e.events[-_LIVE_LOG_CAP:]:
                raw.append((ev, sym))
        raw.sort(key=lambda x: x[0].ts, reverse=True)
        recent = []
        for ev, sym in raw[:_LIVE_LOG_CAP]:
            recent.append({
                "ts": ev.ts, "action": ev.action, "actionColor": _action_color(ev.action),
                "sym": sym, "price": _fmt_price(ev.price) if ev.price else "—",
                "size": (f"{ev.size:.4f}".rstrip("0").rstrip(".")) if ev.size else "—",
                "fee": f"-${ev.fee:.2f}" if ev.fee else "—",
                "pnl": (("+" if ev.pnl >= 0 else "-") + f"${abs(ev.pnl):.2f}") if ev.pnl else "—",
                "pnlColor": "#157a4f" if ev.pnl > 0 else ("#a93529" if ev.pnl < 0 else "#939a93"),
                "comment": ev.comment,
            })

        # шаг цены (tick) из книги — для форматирования лестницы под инструмент (не хардкод)
        tick = None
        if ob and ob.get("a") and len(ob["a"]) >= 2:
            diffs = [round(float(ob["a"][i + 1][0]) - float(ob["a"][i][0]), 10)
                     for i in range(len(ob["a"]) - 1)]
            diffs = sorted(d for d in diffs if d > 0)
            if diffs:
                tick = diffs[0]

        # моя заявка для карточки очереди: ближайшая к mid среди ордеров выбранного движка.
        # ahead — агрегатная оценка очереди по L2 (граница блока C); queue_ahead — оценка движка,
        # тающая по реальным сделкам (для сеточных). behind — глубина моей стороны за моим уровнем.
        my_order = None
        if ob and ob.get("a") and ob.get("b") and se.orders:
            bb, ba = float(ob["b"][0][0]), float(ob["a"][0][0])
            mid_px = (bb + ba) / 2.0 if (bb and ba) else se._last_price
            feat = min(se.orders, key=lambda o: abs(o.price - mid_px))
            levels = ob["b"] if feat.side == "buy" else ob["a"]
            ahead = book_queue_ahead(levels, feat.price, feat.side)
            if feat.side == "buy":
                behind = sum(float(q) for p, q in levels if float(p) < feat.price - 1e-12)
            else:
                behind = sum(float(q) for p, q in levels if float(p) > feat.price + 1e-12)
            my_order = {"side": feat.side, "price": feat.price, "size": feat.size,
                        "ahead": round(ahead, 6), "behind": round(behind, 6),
                        "queue_ahead": round(feat.queue_ahead, 6),
                        "manual": feat.manual,
                        "status": "halted" if se.halted else "queue"}

        # лента РЕАЛЬНЫХ рыночных сделок (поток publicTrade, блок B/E) по выбранному символу.
        # Это рынок целиком (сторона = агрессор), НЕ филлы движка (те — в recent).
        tape = []
        if self.ob_feed and self.ob_feed.symbol == sel:
            for tr in self.ob_feed.recent_trades(40):
                tape.append({"ts": tr.ts, "price": tr.price, "size": tr.size, "side": tr.side})

        return {
            "type": "frame", "live": True, "selected": sel,
            # «started» в кадре — состояние ВЫБРАННОЙ сетки: кнопка вверху
            # управляет именно ей, а не всей корзиной.
            "started": se.active,
            "any_started": self.state["started"],
            "free_cash": round(self.free_cash, 2),
            # Таймфрейм СЕССИИ: по нему считается ATR, и он может не совпадать
            # с масштабом графика — из-за чего шаг «от ATR» выглядит взявшимся ниоткуда.
            "interval": self.interval,
            # Параметры ВЫБРАННОЙ сетки: панель «Параметры» правит её, а не сессию.
            # Плечо отдаём ДЕЙСТВУЮЩЕЕ: в самом параметре 0 означает «как в конфиге»,
            # и показывать человеку ноль вместо реальной тройки нельзя.
            "params": {**se.p.model_dump(), "leverage": round(se.leverage(), 2)},
            "capital": round(self.capital, 2),
            "price": se._last_price,
            "dash": {"balance": round(balance, 2), "equity": round(total_eq, 2),
                     "roi": round(roi, 2), "drawdown": round(dd * 100, 2),
                     "realized": round(total_real, 2), "unrealized": round(total_unreal, 2),
                     "open_orders": open_ord, "fees": round(total_fees, 2),
                     # Число ИСПОЛНЕННЫХ сделок. Счётчика лимиток недостаточно:
                     # он почти всегда стоит на месте (исполнилась заявка — сразу
                     # выставилась парная), и по нему не видно, торгует ли сетка.
                     "trades": sum(e.trades for e in engines.values()),
                     # Позиция и последняя закрытая пара по ВЫБРАННОМУ инструменту:
                     # «что у меня сейчас куплено и почём» и «сколько принесла
                     # последняя закрытая сделка» — то, что человек и хочет видеть.
                     "pos_qty": round(se.pos.qty, 8),
                     "pos_avg": round(se.pos.avg_entry, 6),
                     "last_net": (round(se.roundtrips[-1]["net"], 4)
                                  if se.roundtrips else None),
                     # Плечо, обеспечение и цена ликвидации ВЫБРАННОЙ пары — то,
                     # что на бирже видно рядом с позицией.
                     "leverage": round(se.leverage(), 2),
                     "max_leverage": se.max_leverage,
                     "margin_used": round(se.locked_capital(), 2),
                     "liq_price": round(se.liquidation_price(), 6),
                     "funding": round(total_fund, 2)},
            "instruments": instruments,
            "roundtrips": [
                {"ts": r["ts"], "dir": r["dir"], "entry": round(r["entry"], 6),
                 "exit": round(r["exit"], 6), "qty": round(r["qty"], 8),
                 "gross": round(r["gross"], 4), "fee": round(r["fee"], 4),
                 "net": round(r["net"], 4)}
                for r in se.roundtrips[-120:]][::-1],
            "candles": [[c.o, c.h, c.l, c.c, c.v] for c in tail],
            "times": [int(c.ts // 1000) for c in tail],
            "markers": markers[-_LIVE_LOG_CAP:],
            "manual": manual,
            "grid": grid,
            "grid_info": se.grid_info(),
            "preview": preview,
            "orderbook": ob,
            "tick": tick,
            "my_order": my_order,
            "tape": tape,
            "ob_stats": self.ob_feed.stats() if self.ob_feed else None,
            "archive": self.archiver.stats() if self.archiver else None,
            "mm": se.mm_metrics(),                              # разложение PnL спред/инвентарь, fill-rate, markout (блок F)
            "inventory_stats": _M.inventory_stats(se.inv_curve),
            "inv_curve": [round(x, 8) for x in se.inv_curve[-120:]],
            "equity_curve": _downsample_curve(self.port_curve, 400),    # вся сессия, старт слева
            "balance_curve": _downsample_curve(self.bal_curve, 400),
            "recent": recent,
        }

    # ───────── обслуживание клиента ─────────
    async def serve(self, ws, interval=None, params=None, apply=False):
        await self.ensure_running()
        if interval:
            await self.set_interval(interval)
        # Параметры применяем ТОЛЬКО по явному «Применить · пересчитать» (apply=True).
        # На обычном F5/реконнекте сервер — источник истины, его params (с mode) сохраняются.
        if params is not None and apply:
            self.apply_params(params)
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.subscribers.add(q)
        sender = asyncio.create_task(self._sender(ws, q))
        try:
            await ws.send_json(self.build_frame())   # моментально показать аккаунт (F5)
            while True:
                msg = await ws.receive_json()
                self.handle(msg)
                with contextlib.suppress(Exception):
                    await ws.send_json(self.build_frame())
        except Exception:
            pass
        finally:
            self.subscribers.discard(q)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    async def _sender(self, ws, q):
        try:
            while True:
                frame = await q.get()
                await ws.send_json(frame)
        except Exception:
            return


_SESSION: LiveSession | None = None


def get_session(settings: Settings, params: GridParams, interval: str) -> LiveSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = LiveSession(settings, params, interval)
    return _SESSION


def persist_session():
    if _SESSION is not None:
        _SESSION._persist()


def live_session() -> "LiveSession | None":
    """Текущая живая сессия в памяти (или None) — для экспорта журнала/статистики."""
    return _SESSION


def current_params() -> dict | None:
    """Текущие параметры живой сессии (режим/γ/κ и т.д.) — чтобы UI на первом
    рендере (до ws-кадра) уже показывал верный режим, без мерцания на F5."""
    if _SESSION is not None:
        return _SESSION.params.model_dump()
    # сессии ещё нет в памяти — попробовать прочитать из персиста
    try:
        s = get_settings_safe()
        path = Path(s.db_path).with_name("gridlab_session.json")
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            if data.get("params"):
                return data["params"]
    except Exception:
        pass
    return None


def get_settings_safe():
    from ..config import get_settings
    return get_settings()
