"""Replay-as-Live — проигрывание архива фьючерса как ЖИВОЙ торговли.

Это НЕ батч: цены текут через тот же тиковый конвейер, что и Live (PaperEngine.on_tick +
единый Quoter), бар за баром, с управляемой скоростью. Эквити, позиция, журнал и сетка
обновляются на глазах. Регулятор: скорость / пауза / перемотка / рестарт.

Источник цен за интерфейсом: сейчас локальный архив (Finam-CSV 1m из D:\\DEFI),
позже — ALOR API вторым источником без переписывания фронта/движка.

Модель исполнения (честно, на ценах без стакана):
- строго N контрактов на ордер (Replay = 1), атомарно: цена коснулась уровня → исполнено целиком;
- очередь в стакане НЕ эмулируется (в архиве её нет) — для 1 контракта наименьшее искажение;
- мейкер-комиссия = 0 (ALOR «Срочный Стандарт»), funding нет (фьючерс);
- PnL в рублях через стоимость пункта (point_value из спеки инструмента).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace

from ..config import Settings
from ..marketdata import localdata
from ..models import Candle, GridParams
from ..portfolio.manager import _action_color
from ..strategy.indicators import Indicators
from .costmodel import CostModel
from .futures import SPECS, DEFAULT_CODE, spec_path
from .paper import PaperEngine

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
          "1h": 3_600_000, "4h": 14_400_000}
_WARM = 250          # баров до старта торговли (прогрев индикаторов + стартовая история)
_MAXDISP = 4000      # сколько свечей держим в графике


def _downsample(xs, n=120):
    if len(xs) <= n:
        return list(xs)
    step = len(xs) / n
    return [xs[min(int(i * step), len(xs) - 1)] for i in range(n)]


class ReplaySession:
    """Одна сессия проигрывания на ws-подключение (лаборатория, рестартуемая)."""

    def __init__(self, settings: Settings):
        self.s = settings
        # фьючерсная модель издержек: мейкер 0, funding 0
        base = CostModel.from_settings(settings)
        self.cost = replace(base, maker_fee=0.0, taker_fee=0.0, default_funding_rate=0.0)

        self.code = DEFAULT_CODE
        self.tf = "5m"
        self.params = GridParams(levels=2, max_orders=6, contract_qty=1.0,
                                 grid_spacing=1.8, mode="heuristic")
        self.capital = 100_000.0       # бумажный счёт в рублях
        self.speed = 8.0               # баров в секунду (регулятор)
        self.paused = False

        self.warm: list[Candle] = []      # прогревочный участок (последние _WARM баров до точки)
        self.idx = 0                      # текущий индекс проигрываемого (playable) бара
        self.engine: PaperEngine | None = None
        self.eq_curve: list[float] = []
        self.bal_curve: list[float] = []
        self.disp: list[Candle] = []      # видимое окно свечей (rolling, без всего архива)

        # потоковое чтение архива: не держим весь ряд в памяти, читаем поток + буфер
        self._gen = None                  # текущий генератор свечей из файла
        self._exhausted = False           # поток исчерпан (дошли до конца архива)
        self._tfmin = 5                   # минут в одном баре ТФ
        self._total_lines = 0             # строк данных в файле (для оценки длины)
        self._n = 1                       # оценка числа проигрываемых баров (для прогресса/seek)
        self._tfrom = 0                   # ts первого бара файла (ms)
        self._tto = 0                     # ts последнего бара файла (ms)

        self._cmd: asyncio.Queue = asyncio.Queue()
        self._meta: dict = {}

    # ───────── построение/рестарт ─────────
    def _spec(self):
        return SPECS.get(self.code) or SPECS[DEFAULT_CODE]

    def _load(self):
        """Лёгкая инициализация: считаем строки (для длины/seek) и границы времени.
        Сам архив НЕ загружаем в память — играем потоком из файла."""
        path = spec_path(self.code)
        self._tfmin = max(1, _TF_MS.get(self.tf, 300_000) // 60_000)
        self._total_lines = localdata.count_data_lines(path)
        self._tfrom, self._tto = localdata.first_last_ts(path)
        self._n = max(1, self._total_lines // self._tfmin - _WARM)

    def _start_at(self, skip_lines: int, idx: int):
        """Открыть поток с позиции skip_lines: прогреть движок на _WARM барах, начать
        торговать дальше. Память — только прогрев + окно, без всего архива."""
        path = spec_path(self.code)
        gen = localdata.stream_candles(path, self.tf, max(0, skip_lines))
        warm = []
        for _ in range(_WARM):
            try:
                warm.append(next(gen))
            except StopIteration:
                break
        self._gen = gen
        self._exhausted = False
        self.warm = warm
        spec = self._spec()
        e = PaperEngine(self.code, self.capital, self.params, self.cost,
                        funding=None, mult=spec.point_value)
        e.warm(warm)
        e.start_strategy(warm[-1].c if warm else 0.0)
        self.engine = e
        self.eq_curve = [round(self.capital, 2)]
        self.bal_curve = [round(self.capital, 2)]
        self.disp = list(warm[-_MAXDISP:])
        self.idx = max(0, idx)

    def rebuild(self, reset_idx: bool = True):
        """Полная пересборка (смена инструмента/ТФ/рестарт) — поток с начала архива."""
        self._load()
        self._start_at(0, 0)

    def seek(self, target: int):
        """Перемотать к проигрываемому бару target: открыть поток с этой позиции, прогреть
        индикаторы на _WARM барах перед ней и начать торговать оттуда (свежий счёт).
        Быстро, без переигрывания и без загрузки всего архива."""
        target = max(0, min(int(target), self._n))
        self._start_at(target * self._tfmin, target)

    # ───────── проигрывание одного бара тик-за-тиком ─────────
    def _play_bar(self, bar: Candle, prev: Candle | None, sample: bool = True):
        e = self.engine
        # порядок внутри бара честно неизвестен: идём от open к ближнему экстремуму,
        # затем к дальнему, затем close — реалистичные касания уровней сетки
        up = bar.c >= bar.o
        path = [bar.o, bar.l, bar.h, bar.c] if up else [bar.o, bar.h, bar.l, bar.c]
        for px in path:
            e.on_tick(px, bar.ts)
            if sample:
                eq = e.equity()
                self.eq_curve.append(round(eq, 2))
                self.bal_curve.append(round(e.cash, 2))
        if not sample:
            self.eq_curve.append(round(e.equity(), 2))
            self.bal_curve.append(round(e.cash, 2))
        if len(self.eq_curve) > 1200:
            self.eq_curve = self.eq_curve[-1200:]
            self.bal_curve = self.bal_curve[-1200:]
        # индикаторы обновляем ПОСЛЕ тиков бара (квотирование шло на ATR до этого бара)
        e.ind.update(bar)

    # ───────── кадр для фронта (₽) ─────────
    def frame(self, kind: str = "frame") -> dict:
        e = self.engine
        spec = self._spec()
        disp = self.disp
        first_ts = disp[0].ts if disp else 0
        markers = []
        for f in (e.fills[-400:] if e else []):
            if f.ts >= first_ts:   # метка по ВРЕМЕНИ — переживает зум/прокрутку всей истории
                markers.append({"time": int(f.ts // 1000), "side": f.side, "price": f.price})
        grid = [{"side": o.side, "price": o.price, "size": o.size, "level": o.level}
                for o in (e.orders if e else []) if not o.manual]
        manual = [{"oid": o.oid, "side": o.side, "price": o.price, "size": o.size}
                  for o in (e.orders if e else []) if o.manual]
        # превью следующих уровней
        preview = []
        if e and e._last_price > 0:
            step = e._grid_step(e._last_price)
            gb = [o.price for o in e.orders if not o.manual and o.side == "buy"]
            gs = [o.price for o in e.orders if not o.manual and o.side == "sell"]
            if gb:
                preview.append({"side": "buy", "price": min(gb) - step})
            if gs:
                preview.append({"side": "sell", "price": max(gs) + step})
        recent = []
        for ev in (e.events[-50:] if e else [])[::-1]:
            recent.append({
                "ts": ev.ts, "action": ev.action, "actionColor": _action_color(ev.action),
                "price": _fmt_pts(ev.price, spec.price_step) if ev.price else "—",
                "size": (f"{ev.size:g}") if ev.size else "—",
                "pnl": (("+" if ev.pnl >= 0 else "−") + f"{abs(ev.pnl):,.0f} ₽") if ev.pnl else "—",
                "pnlColor": "#157a4f" if ev.pnl > 0 else ("#a93529" if ev.pnl < 0 else "#939a93"),
                "comment": ev.comment,
            })
        eq = e.equity() if e else self.capital
        roi = (eq - self.capital) / self.capital * 100.0 if self.capital else 0.0
        peak, dd = (self.eq_curve[0] if self.eq_curve else eq), 0.0
        for v in self.eq_curve:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak)
        gi = e.grid_info() if e else {}
        cur = disp[-1] if disp else None
        return {
            "type": kind, "code": self.code, "tf": self.tf,
            "idx": self.idx, "n": self._n,
            "playing": (not self.paused) and not self._exhausted,
            "paused": self.paused, "speed": self.speed,
            "bar_time": int(cur.ts // 1000) if cur else 0,
            "spec": {"label": spec.label, "point_value": spec.point_value,
                     "price_step": spec.price_step, "approx": spec.approx, "note": spec.note},
            "params": self.params.model_dump(),
            "capital": round(self.capital, 2),
            "price": e._last_price if e else 0.0,
            "dash": {
                "balance": round(e.cash, 2) if e else self.capital,
                "equity": round(eq, 2),
                "roi": round(roi, 2), "drawdown": round(dd * 100, 2),
                "realized": round(e.pos.realized, 2) if e else 0.0,
                "unrealized": round(e.unrealized_money(e._last_price), 2) if e else 0.0,
                "position": round(e.pos.qty, 4) if e else 0.0,
                "long_filled": round(e.buy_filled, 4) if e else 0.0,
                "short_filled": round(e.sell_filled, 4) if e else 0.0,
                "avg_entry": (_fmt_pts(e.pos.avg_entry, spec.price_step) if e and e.pos.qty else "—"),
                "trades": e.trades if e else 0,
                "open_orders": len([o for o in e.orders if not o.manual]) if e else 0,
                "fees": round(e.pos.fees_paid, 2) if e else 0.0,
            },
            "candles": [[c.o, c.h, c.l, c.c, c.v] for c in disp],
            "times": [int(c.ts // 1000) for c in disp],
            "markers": markers[-80:],
            "grid": grid, "manual": manual, "preview": preview, "grid_info": gi,
            "equity_curve": [round(v, 2) for v in self.eq_curve[-400:]],
            "balance_curve": [round(v, 2) for v in self.bal_curve[-400:]],
            "recent": recent,
        }

    def history_frame(self, before_ms: int, count: int) -> dict:
        """Более старые свечи (ts < before_ms) — для прокрутки графика назад. Находим
        позицию бинарным поиском по файлу и читаем кусок потоком (без загрузки архива)."""
        if before_ms <= 0:
            return {"type": "history", "candles": [], "times": [], "exhausted": True}
        out = localdata.history_chunk(spec_path(self.code), self.tf, before_ms, count)
        return {"type": "history",
                "candles": [[c.o, c.h, c.l, c.c, c.v] for c in out],
                "times": [int(c.ts // 1000) for c in out],
                "exhausted": bool(out) and out[0].ts <= self._tfrom}

    def future_frame(self, after_ms: int, count: int) -> dict:
        """Следующие свечи (ts > after_ms) — для прокрутки графика ВПЕРЁД (предпросмотр).
        Только показ; робот вперёд не торгует (торговля — лишь при проигрывании)."""
        if after_ms <= 0:
            return {"type": "future", "candles": [], "times": [], "exhausted": True}
        out = localdata.future_chunk(spec_path(self.code), self.tf, after_ms, count)
        return {"type": "future",
                "candles": [[c.o, c.h, c.l, c.c, c.v] for c in out],
                "times": [int(c.ts // 1000) for c in out],
                "exhausted": bool(out) and out[-1].ts >= self._tto}

    # ───────── команды от клиента ─────────
    def apply_cmd(self, msg: dict) -> bool:
        """Вернёт True, если нужна моментальная отправка кадра."""
        act = msg.get("action")
        if act == "speed":
            self.speed = max(0.25, min(float(msg.get("speed", self.speed)), 240.0))
            return True
        elif act == "pause":
            self.paused = True
            return True
        elif act == "play":
            self.paused = False
            return True
        elif act == "restart":
            self.rebuild(reset_idx=True)
            self.paused = msg.get("paused", self.paused)   # сохраняем текущую паузу (не авто-старт)
            return True
        elif act == "seek":
            frac = float(msg.get("frac", 0))
            self.seek(int(frac * self._n))
            return True
        elif act == "step":   # шаг на 1 бар (когда на паузе)
            self.paused = True
            self._advance_one()
            return True
        elif act == "set_instrument":
            self.code = msg.get("code", self.code)
            self.rebuild(reset_idx=True)
            return True
        elif act == "set_tf":
            self.tf = msg.get("tf", self.tf)
            self.rebuild(reset_idx=True)
            return True
        elif act == "apply_params":
            with contextlib.suppress(Exception):
                self.params = GridParams(**{**msg.get("params", {}),
                                            "contract_qty": 1.0})
                # пересобрать с текущего места: перемотать заново под новые параметры
                self.seek(self.idx)
                return True
        elif act == "set_capital":
            with contextlib.suppress(Exception):
                v = float(msg.get("capital", self.capital))
                if v > 0:
                    self.capital = v
                    self.seek(self.idx)
                    return True
        elif act == "place_order":
            e = self.engine
            if e and msg.get("price") and msg.get("qty"):
                with contextlib.suppress(Exception):
                    e.add_manual_order(msg.get("side", "buy"), float(msg["price"]), float(msg["qty"]))
                    return True
        elif act == "cancel_order":
            e = self.engine
            if e and msg.get("oid") is not None:
                with contextlib.suppress(Exception):
                    e.cancel_manual(int(msg["oid"]))
                    return True
        return False

    def _advance_one(self) -> bool:
        if self._exhausted or self._gen is None:
            return False
        try:
            bar = next(self._gen)
        except StopIteration:
            self._exhausted = True
            return False
        prev = self.disp[-1] if self.disp else (self.warm[-1] if self.warm else None)
        self._play_bar(bar, prev, sample=True)
        self.disp.append(bar)
        if len(self.disp) > _MAXDISP:
            self.disp.pop(0)
        self.idx += 1
        return True

    # ───────── главный цикл проигрывания ─────────
    async def serve(self, ws):
        try:
            cfg = await ws.receive_json()
        except Exception:
            return
        self.code = cfg.get("code", self.code)
        self.tf = cfg.get("tf", self.tf)
        self.speed = float(cfg.get("speed", self.speed))
        if cfg.get("bars"):
            self._meta["bars"] = int(cfg["bars"])
        if cfg.get("params"):
            with contextlib.suppress(Exception):
                self.params = GridParams(**{**cfg["params"], "contract_qty": 1.0})
        if cfg.get("capital"):
            with contextlib.suppress(Exception):
                self.capital = float(cfg["capital"])
        # тяжёлая загрузка/ресэмпл архива — в потоке, чтобы не блокировать event-loop
        # (иначе ws keepalive ping отваливается на больших файлах при первой загрузке)
        await asyncio.to_thread(self.rebuild, True)
        self.paused = bool(cfg.get("paused", False))

        recv = asyncio.create_task(self._recv_loop(ws))
        try:
            await ws.send_json(self.frame())
            last_emit = 0.0
            while True:
                # обработать накопленные команды (тяжёлые — смена инструмента/ТФ/seek — в потоке)
                forced = False
                while not self._cmd.empty():
                    msg = self._cmd.get_nowait()
                    if msg.get("action") == "more_history":
                        hf = self.history_frame(int(msg.get("before_ts", 0)), int(msg.get("count", 500)))
                        await ws.send_json(hf)
                        continue
                    if msg.get("action") == "more_future":
                        ff = self.future_frame(int(msg.get("after_ts", 0)), int(msg.get("count", 500)))
                        await ws.send_json(ff)
                        continue
                    if await asyncio.to_thread(self.apply_cmd, msg):
                        forced = True
                if forced:
                    await ws.send_json(self.frame())
                    last_emit = time.monotonic()

                if self.paused or self._exhausted:
                    await asyncio.sleep(0.05)
                    if self._exhausted and not self.paused:
                        self.paused = True
                        await ws.send_json(self.frame("done"))
                    continue

                if not self._advance_one():
                    continue
                # частота кадров ограничена ~25/с; при высокой скорости — несколько баров на кадр
                now = time.monotonic()
                if now - last_emit >= 0.04:
                    await ws.send_json(self.frame())
                    last_emit = now
                delay = 1.0 / max(self.speed, 0.25)
                if delay > 0.005:
                    await asyncio.sleep(delay)
        except Exception:
            pass
        finally:
            recv.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recv

    async def _recv_loop(self, ws):
        try:
            while True:
                msg = await ws.receive_json()
                await self._cmd.put(msg)
        except Exception:
            return


def _fmt_pts(v: float, step: float) -> str:
    if not v:
        return "—"
    if step >= 1:
        return f"{v:,.0f}"
    if step >= 0.01:
        return f"{v:.2f}"
    return f"{v:.4f}"
