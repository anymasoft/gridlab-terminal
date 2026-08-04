"""GRIDLAB — FastAPI: отдача SPA + REST + WebSocket-стрим (live paper / replay).

Этапность (как в брифе): paper-движок на реальных свечах Bybit → бэктест и live →
sweep для теста универсальности параметров → экспорт. Testnet — за VENUE/venue-фабрикой.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from . import storage
from .config import get_settings
from .engine.costmodel import CostModel
from .engine.paper import PaperEngine
from .engine.venue import get_venue
from .marketdata import bybit
from .models import GridParams, LabRequest, RunRequest, SweepRequest
from .portfolio import manager
from .backtest import lab
from .backtest import optimizer
from .marketdata import localdata

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gridlab")

app = FastAPI(title="GRIDLAB")
WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

# кэш свечей и последнего прогона (чтобы UI и стрим не тянули заново)
_cache: dict[str, dict] = {}


@app.on_event("startup")
async def _startup() -> None:
    s = get_settings()
    storage.init(s.db_path)
    log.info("GRIDLAB запущен. venue=%s · символов=%d · интервал=%s",
             s.resolve_venue(), len(s.symbols), s.default_interval)
    # Автостарт постоянной сессии: торговля идёт даже без открытого браузера
    # (сделки исполняются по тикам Bybit). Восстанавливает счёт/ордера/режим из файла.
    from .engine.session import get_session
    sess = get_session(s, GridParams(), s.default_interval)
    await sess.ensure_running()


@app.on_event("shutdown")
async def _shutdown() -> None:
    from .engine.session import persist_session
    persist_session()   # сохранить аккаунт (ордера/позиции) при остановке сервера


# ─────────────────────────── статика ───────────────────────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/app.js")
async def appjs() -> FileResponse:
    return FileResponse(os.path.join(WEB, "app.js"), media_type="application/javascript")


@app.get("/lab")
async def lab_page() -> FileResponse:
    return FileResponse(os.path.join(WEB, "lab.html"), media_type="text/html")


@app.get("/lab.js")
async def lab_js() -> FileResponse:
    return FileResponse(os.path.join(WEB, "lab.js"), media_type="application/javascript")


@app.get("/replay")
async def replay_page() -> FileResponse:
    return FileResponse(os.path.join(WEB, "replay.html"), media_type="text/html")


@app.get("/replay.js")
async def replay_js() -> FileResponse:
    return FileResponse(os.path.join(WEB, "replay.js"), media_type="application/javascript")


@app.get("/api/replay/instruments")
async def api_replay_instruments() -> JSONResponse:
    from .engine.futures import list_specs, DEFAULT_CODE
    return JSONResponse({"instruments": list_specs(), "default": DEFAULT_CODE,
                         "tfs": ["1m", "5m", "15m", "30m", "1h"]})


@app.get("/api/replay/data_info")
async def api_replay_data_info(code: str = "Si") -> JSONResponse:
    from .engine.futures import SPECS, DEFAULT_CODE, spec_path
    spec = SPECS.get(code) or SPECS[DEFAULT_CODE]
    meta = await asyncio.to_thread(localdata.file_info, spec_path(code), code)
    meta.update({"label": spec.label, "point_value": spec.point_value,
                 "price_step": spec.price_step, "approx": spec.approx, "note": spec.note})
    return JSONResponse(meta)


@app.websocket("/ws/replay")
async def ws_replay(ws: WebSocket) -> None:
    """Живое проигрывание архива фьючерса (Replay-as-Live) с регулятором скорости."""
    await ws.accept()
    from .engine.replay import ReplaySession
    sess = ReplaySession(get_settings())
    try:
        await sess.serve(ws)
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        import contextlib
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "msg": str(e)})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def _live_params() -> dict | None:
    from .engine.session import current_params
    return current_params()


@app.get("/api/config")
async def api_config() -> dict:
    s = get_settings()
    cost = CostModel.from_settings(s)
    return {
        "symbols": s.symbols,
        "interval": s.default_interval,
        "venue": s.resolve_venue(),
        "venue_requested": s.venue,
        "has_testnet_keys": s.has_testnet_keys,
        "start_capital": s.start_capital,
        "defaults": GridParams().model_dump(),
        "live_params": _live_params(),   # текущий режим/параметры сессии — чтобы UI не мерцал на F5
        "cost": {
            "maker_bps": s.maker_fee_bps, "taker_bps": s.taker_fee_bps,
            "slippage_bps": s.slippage_bps, "leverage": s.leverage,
            "funding_interval_h": s.funding_interval_h,
        },
    }


# ─────────────────────────── helpers ───────────────────────────
async def _load_candles(symbols: list[str], interval: str) -> dict:
    s = get_settings()
    cmap = await bybit.fetch_many_klines(symbols, interval, s.history_bars)
    fmap = {}
    for sym in cmap:
        fmap[sym] = await bybit.fetch_funding(sym)
    return {"candles": cmap, "funding": fmap}


def _build_charts(candles_map: dict, result: dict, n: int = 150) -> dict:
    """Данные для Canvas-графика цены: tail OHLC + маркеры grid-исполнений по ts."""
    charts = {}
    fills_by_sym = {i["sym"]: i.get("fills", []) for i in result["instruments"]}
    for sym, cs in candles_map.items():
        tail = cs[-n:]
        if not tail:
            continue
        ts_arr = [c.ts for c in tail]
        markers = []
        for f in fills_by_sym.get(sym, []):
            j = bisect.bisect_left(ts_arr, f["ts"])
            j = min(max(j, 0), len(tail) - 1)
            markers.append({"i": j, "side": f["side"], "price": f["price"]})
        charts[sym] = {
            "candles": [[c.o, c.h, c.l, c.c, c.v] for c in tail],
            "markers": markers,
        }
    return charts


# ─────────────────────────── бэктест ───────────────────────────
@app.post("/api/backtest")
async def api_backtest(req: RunRequest) -> JSONResponse:
    s = get_settings()
    symbols = req.symbols or s.symbols
    interval = req.interval or s.default_interval
    base = req.params or GridParams()

    loaded = await _load_candles(symbols, interval)
    cmap = loaded["candles"]
    if not cmap:
        return JSONResponse({"error": "Не удалось загрузить свечи Bybit"}, status_code=502)

    result = manager.run_portfolio(cmap, s, base, req.per_symbol,
                                   loaded["funding"], interval)
    result["charts"] = _build_charts(cmap, result)
    result["params"] = base.model_dump()
    result["venue"] = s.resolve_venue()
    rid = storage.save_run(result, base.mode, s.resolve_venue())
    result["run_id"] = rid
    _cache["last"] = result
    _cache["candles"] = cmap
    _cache["funding"] = loaded["funding"]
    return JSONResponse(result)


@app.get("/api/init")
async def api_init(interval: str | None = None) -> JSONResponse:
    """Свечи + аллокации БЕЗ запуска торговли. Терминал рисует график и портфель,
    но сделок нет, пока пользователь не нажал старт (Бэктест/Replay/Live)."""
    s = get_settings()
    interval = interval or s.default_interval
    loaded = await _load_candles(s.symbols, interval)
    cmap = loaded["candles"]
    if not cmap:
        return JSONResponse({"error": "Не удалось загрузить свечи Bybit"}, status_code=502)
    allocs = manager.risk_parity_alloc(cmap, s.start_capital)
    insts = []
    for sym, cs in cmap.items():
        tail = cs[-150:]
        insts.append({
            "sym": sym, "alloc": allocs[sym], "last": cs[-1].c,
            "candles": [[c.o, c.h, c.l, c.c, c.v] for c in tail],
            "times": [int(c.ts // 1000) for c in tail],
            "price_spark": [c.c for c in cs[-22:]],
        })
    _cache["candles"] = cmap
    _cache["funding"] = loaded["funding"]
    return JSONResponse({"interval": interval, "instruments": insts,
                         "start_capital": s.start_capital, "symbols": s.symbols})


@app.get("/api/last")
async def api_last() -> JSONResponse:
    r = _cache.get("last") or storage.latest_run()
    if not r:
        return JSONResponse({"empty": True})
    return JSONResponse(r)


# ─────────────────── sweep: тест универсальности k ───────────────────
@app.post("/api/sweep")
async def api_sweep(req: SweepRequest) -> JSONResponse:
    """Прогон по сетке k (grid_spacing) на одном инструменте → ROI по каждому k.
    Видно, сходится ли оптимум k к единому безразмерному значению (защита от оверфита)."""
    s = get_settings()
    interval = req.interval or s.default_interval
    base = req.base or GridParams()
    cost = CostModel.from_settings(s)
    candles = await bybit.fetch_klines(req.symbol, interval, s.history_bars)
    funding = await bybit.fetch_funding(req.symbol)
    if not candles:
        return JSONResponse({"error": "нет свечей"}, status_code=502)
    alloc = s.start_capital / max(len(s.symbols), 1)

    points = []
    k = req.k_min
    best = None
    while k <= req.k_max + 1e-9:
        p = base.model_copy(update={"grid_spacing": round(k, 3)})
        eng = PaperEngine(req.symbol, alloc, p, cost, funding)
        eng.run(candles)
        sm = eng.summary()
        pt = {"k": round(k, 3), "roi": round(sm["pnl_pct"], 2),
              "trades": sm["trades"], "liquidated": sm["liquidated"]}
        points.append(pt)
        if best is None or pt["roi"] > best["roi"]:
            best = pt
        k += req.k_step
    return JSONResponse({"symbol": req.symbol, "interval": interval,
                         "points": points, "best_k": best["k"] if best else None,
                         "best_roi": best["roi"] if best else None})


# ─────────────────── Optimization Lab (бэктест на ETHBTC/Bybit) ───────────────────
async def _lab_candles(req: LabRequest):
    if req.source == "local":
        return await asyncio.to_thread(localdata.get_candles, req.tf, None, None, None, req.bars), True
    s = get_settings()
    candles = await bybit.fetch_klines(req.symbol, req.tf, min(req.bars, 1000))
    return candles, False


def _lab_base(req: LabRequest) -> GridParams:
    return req.base or GridParams(levels=2, max_orders=2)


@app.get("/api/lab/data_info")
async def api_lab_data_info() -> JSONResponse:
    info = await asyncio.to_thread(localdata.info)
    info["tfs"] = ["5m", "15m", "30m", "1h", "4h"]
    return JSONResponse(info)


@app.post("/api/lab/single")
async def api_lab_single(req: LabRequest) -> JSONResponse:
    s = get_settings()
    candles, spot = await _lab_candles(req)
    if not candles:
        return JSONResponse({"error": "нет свечей"}, status_code=502)
    cost = CostModel.from_settings(s)
    m = await asyncio.to_thread(lab.run_backtest, candles, _lab_base(req), req.capital, cost, spot)
    return JSONResponse({"metrics": m, "spot": spot, "bars": len(candles),
                         "from": candles[0].ts, "to": candles[-1].ts})


@app.post("/api/lab/sweep")
async def api_lab_sweep(req: LabRequest) -> JSONResponse:
    s = get_settings()
    candles, spot = await _lab_candles(req)
    if not candles:
        return JSONResponse({"error": "нет свечей"}, status_code=502)
    grid = req.grid or {"grid_spacing": [1.0, 1.5, 2.0, 2.5, 3.0], "sl": [3.0, 4.0, 5.0]}
    cost = CostModel.from_settings(s)
    out = await asyncio.to_thread(lab.sweep, candles, grid, _lab_base(req),
                                  req.capital, cost, req.weights, spot)
    out["bars"] = len(candles)
    _cache["lab_sweep"] = out
    return JSONResponse(out)


@app.post("/api/lab/heatmap")
async def api_lab_heatmap(req: LabRequest) -> JSONResponse:
    s = get_settings()
    candles, spot = await _lab_candles(req)
    if not candles:
        return JSONResponse({"error": "нет свечей"}, status_code=502)
    xkey = req.xkey or "grid_spacing"
    ykey = req.ykey or "sl"
    xvals = req.xvals or [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
    yvals = req.yvals or [2.0, 3.0, 4.0, 5.0, 6.0]
    cost = CostModel.from_settings(s)
    out = await asyncio.to_thread(lab.heatmap, candles, xkey, xvals, ykey, yvals,
                                  _lab_base(req), req.capital, cost, req.metric, req.weights, spot)
    out["bars"] = len(candles)
    return JSONResponse(out)


@app.post("/api/lab/walkforward")
async def api_lab_walkforward(req: LabRequest) -> JSONResponse:
    s = get_settings()
    candles, spot = await _lab_candles(req)
    if not candles:
        return JSONResponse({"error": "нет свечей"}, status_code=502)
    grid = req.grid or {"grid_spacing": [1.0, 1.5, 2.0, 2.5, 3.0], "sl": [3.0, 4.0, 5.0]}
    cost = CostModel.from_settings(s)
    out = await asyncio.to_thread(lab.walk_forward, candles, grid, _lab_base(req),
                                  req.capital, cost, req.train_days, req.test_days,
                                  req.weights, spot, req.max_windows)
    return JSONResponse(out)


# ─────────────────── Parameter Optimizer (WebSocket) ───────────────────
@app.websocket("/ws/optimizer")
async def ws_optimizer(ws: WebSocket) -> None:
    await ws.accept()
    try:
        cfg = await ws.receive_json()
    except Exception:
        await ws.close()
        return

    code = cfg.get("code", "Si")
    tf = cfg.get("tf", "5m")
    bars = cfg.get("bars", 4000)
    capital = float(cfg.get("capital", 100000))
    base = GridParams(**(cfg.get("base") or {}))
    ranges = cfg.get("ranges", {})
    mode = cfg.get("search_mode", "full")
    preset = cfg.get("preset", "robust")
    n_random = cfg.get("n_random", 500)
    current = cfg.get("current")
    heatmap_x = cfg.get("heatmap_x")
    heatmap_y = cfg.get("heatmap_y")

    # загрузка локальных архивов текущего инструмента
    from .engine.futures import spec_path
    path = spec_path(code)
    candles = await asyncio.to_thread(localdata.get_candles, tf, None, None, path, bars)
    spot = True

    if not candles:
        await ws.send_json({"type": "error", "msg": "нет свечей"})
        await ws.close()
        return

    combos = optimizer.generate_combos(mode, ranges, current, n_random)
    n = len(combos)
    workers = max(1, (os.cpu_count() or 2) - 1)
    await ws.send_json({"type": "start", "total": n, "workers": workers})

    s = get_settings()
    cost = CostModel.from_settings(s)

    # запуск в отдельном потоке с прогрессом через Queue
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _run_in_thread():
        from multiprocessing import Pool
        candles_data = [(c.ts, c.o, c.h, c.l, c.c, c.v) for c in candles]
        cost_dict = {
            "maker_fee": cost.maker_fee, "taker_fee": cost.taker_fee,
            "slippage": cost.slippage, "leverage": cost.leverage,
            "maint_margin": cost.maint_margin,
            "default_funding_rate": cost.default_funding_rate,
            "funding_interval_ms": cost.funding_interval_ms,
        }
        base_dict = base.model_dump()
        tasks = [(i, combo, base_dict) for i, combo in enumerate(combos)]
        chunk = max(1, min(64, len(tasks) // (workers * 4)))
        with Pool(
            processes=workers,
            initializer=optimizer._init_worker,
            initargs=(candles_data, capital, cost_dict, spot),
        ) as pool:
            for r in pool.imap_unordered(optimizer._run_worker, tasks, chunksize=chunk):
                r["score"] = optimizer.score_result(r["metrics"], preset)
                loop.call_soon_threadsafe(q.put_nowait, r)
        loop.call_soon_threadsafe(q.put_nowait, None)  # sentinel

    import threading
    result_holder = {}

    def _thread_target():
        try:
            result_holder["results"] = _run_in_thread()
        except Exception as e:
            import traceback; traceback.print_exc()
            loop.call_soon_threadsafe(q.put_nowait, None)

    t = threading.Thread(target=_thread_target, daemon=True)
    t.start()

    # стримим прогресс в реальном времени
    import heapq, time as _time
    received = 0
    all_results: list[dict] = []
    top20: list[tuple] = []  # min-heap of (score, idx, item)
    cancelled = False
    _last_send = 0.0
    _idx = 0
    while True:
        try:
            item = await asyncio.wait_for(q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if not t.is_alive() and q.empty():
                break
            continue
        if item is None:
            break
        all_results.append(item)
        received += 1
        sc = item["score"]
        if len(top20) < 20:
            heapq.heappush(top20, (sc, _idx, item))
        elif sc > top20[0][0]:
            heapq.heapreplace(top20, (sc, _idx, item))
        _idx += 1
        # drain queue without blocking
        while not q.empty():
            try:
                item2 = q.get_nowait()
            except Exception:
                break
            if item2 is None:
                break
            all_results.append(item2)
            received += 1
            sc2 = item2["score"]
            if len(top20) < 20:
                heapq.heappush(top20, (sc2, _idx, item2))
            elif sc2 > top20[0][0]:
                heapq.heapreplace(top20, (sc2, _idx, item2))
            _idx += 1

        now = _time.monotonic()
        if now - _last_send >= 1.0 or received == n:
            _last_send = now
            snap = sorted(top20, key=lambda x: -x[0])
            snap_list = [{"combo": r[2]["combo"], "metrics": r[2]["metrics"], "score": r[2]["score"]}
                         for r in snap]
            try:
                await ws.send_json({"type": "result", "i": received, "n": n, "top": snap_list})
            except Exception:
                cancelled = True
                break

    if cancelled:
        return

    t.join()
    results = all_results
    results.sort(key=lambda x: x["score"], reverse=True)

    # robustness анализ для топ-20
    top_robust = optimizer.robustness_analysis(results, ranges, top_n=20)

    # heatmap данные
    heatmap_data = None
    if heatmap_x and heatmap_y and heatmap_x in ranges and heatmap_y in ranges:
        xvals = sorted(set(ranges[heatmap_x]))
        yvals = sorted(set(ranges[heatmap_y]))
        grid_map = {}
        roi_map = {}
        for r in results:
            xv = r["combo"].get(heatmap_x)
            yv = r["combo"].get(heatmap_y)
            if xv is not None and yv is not None:
                key = (xv, yv)
                if key not in grid_map or r["score"] > grid_map[key]:
                    grid_map[key] = r["score"]
                    roi_map[key] = r["metrics"]["roi"]
        heatmap_grid = []
        heatmap_roi = []
        for yv in yvals:
            row_s, row_r = [], []
            for xv in xvals:
                row_s.append(round(grid_map.get((xv, yv), -999), 3))
                row_r.append(round(roi_map.get((xv, yv), 0), 3))
            heatmap_grid.append(row_s)
            heatmap_roi.append(row_r)
        heatmap_data = {"xkey": heatmap_x, "ykey": heatmap_y,
                        "xvals": xvals, "yvals": yvals,
                        "grid": heatmap_grid, "roi": heatmap_roi}

    try:
        await ws.send_json({
            "type": "done",
            "total": n,
            "top": top_robust,
            "heatmap": heatmap_data,
            "preset": preset,
        })
    except Exception:
        pass
    await ws.close()


# ─────────────────────────── экспорт ───────────────────────────
@app.get("/api/export")
async def api_export(fmt: str = "json", kind: str = "log") -> Response:
    r = _cache.get("last") or storage.latest_run()
    if not r:
        return JSONResponse({"error": "нет прогона"}, status_code=404)
    if fmt == "csv":
        data = storage.export_csv(r, kind)
        return PlainTextResponse(data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="gridlab_{kind}.csv"'})
    return PlainTextResponse(storage.export_json(r), media_type="application/json",
                             headers={"Content-Disposition": 'attachment; filename="gridlab_run.json"'})


@app.get("/api/live/export")
async def api_live_export(fmt: str = "csv", kind: str = "events") -> Response:
    """Экспорт ЖИВОЙ сессии (paper-аккаунт), а НЕ бэктеста: полный журнал событий или
    лог филлов (CSV), либо весь снимок со статистикой и кривыми (JSON). Источник —
    сессия из памяти (live_session()), история без обрезки кадра."""
    from .engine.session import live_session
    sess = live_session()
    if sess is None or not sess.engines:
        return JSONResponse({"error": "нет живой сессии (live не запускался)"}, status_code=404)
    sel = sess.state.get("selected")
    e = sess.engines.get(sel)
    if e is None:
        return JSONResponse({"error": "нет движка по выбранному символу"}, status_code=404)
    if fmt == "csv":
        if kind == "fills":
            data, fname = storage.live_fills_csv(e.fills, sel), "gridlab_live_fills.csv"
        else:
            data, fname = storage.live_events_csv(e.events, sel), "gridlab_live_events.csv"
        return PlainTextResponse(data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{fname}"'})
    meta = {"symbol": sel, "capital": round(sess.capital, 2),
            "started": sess.state.get("started"), "params": sess.params.model_dump(),
            "exported_ms": int(time.time() * 1000)}
    data = storage.live_bundle_json(
        meta, e.summary(), e.mm_metrics(), e.events, e.fills, sel,
        [round(x, 4) for x in sess.port_curve], [round(x, 4) for x in sess.bal_curve])
    return PlainTextResponse(data, media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="gridlab_live.json"'})


# ───────────────── WebSocket: live paper / replay ─────────────────
@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    """Стрим прогона по барам: replay (по кэшированным свечам с ускорением) либо
    live (дотягиваем свежие свечи Bybit). Шлём кадры эквити/цены/события."""
    await ws.accept()
    s = get_settings()
    try:
        cfg = await ws.receive_json()
    except Exception:
        await ws.close()
        return

    mode = cfg.get("mode", "replay")        # replay | live
    speed = float(cfg.get("speed", 5))
    symbol = cfg.get("symbol") or s.symbols[0]
    interval = cfg.get("interval") or s.default_interval
    params = GridParams(**cfg["params"]) if cfg.get("params") else GridParams()
    cost = CostModel.from_settings(s)

    candles = _cache.get("candles", {}).get(symbol)
    if not candles:
        candles = await bybit.fetch_klines(symbol, interval, s.history_bars)
    funding = _cache.get("funding", {}).get(symbol) or await bybit.fetch_funding(symbol)
    alloc = s.start_capital / max(len(s.symbols), 1)
    eng = PaperEngine(symbol, alloc, params, cost, funding)

    try:
        if mode == "replay":
            delay = max(0.01, 0.12 / max(speed, 0.1))
            eq = []
            for idx, c in enumerate(candles):
                eng.step(c)
                eq.append(round(eng.equity(), 2))
                if idx % 2 == 0 or idx == len(candles) - 1:
                    await ws.send_json({
                        "type": "frame", "i": idx, "n": len(candles),
                        "price": c.c, "equity": eq[-1],
                        "curve": eq[-160:],
                        "summary": eng.summary(),
                        "recent": [ {"action": e.action, "price": e.price,
                                     "pnl": e.pnl, "comment": e.comment}
                                    for e in eng.events[-6:] ],
                    })
                    await asyncio.sleep(delay)
            await ws.send_json({"type": "done", "summary": eng.summary()})
        else:  # live: постоянная серверная сессия (аккаунт как на бирже)
            from .engine.session import get_session
            sess = get_session(s, params, interval)
            await sess.serve(ws, interval=interval, params=params, apply=bool(cfg.get("apply")))
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "msg": str(e)})
        except Exception:
            pass
