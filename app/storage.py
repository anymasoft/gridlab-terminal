"""Хранилище прогонов (SQLite, stdlib) + экспорт CSV/JSON. Сохраняем результат
бэктеста целиком (JSON-блоб) для воспроизводимости и выгрузки сделок/журнала."""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import sqlite3
import time
from typing import Optional

_MSK = datetime.timezone(datetime.timedelta(hours=3))


def _msk(ts: int) -> str:
    """ms-эпоха → строка времени МСК для человекочитаемого экспорта."""
    try:
        return datetime.datetime.fromtimestamp(ts / 1000, tz=_MSK).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return ""

_DB: Optional[str] = None


def init(db_path: str) -> None:
    global _DB
    _DB = db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = _conn()
    con.execute("""CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, mode TEXT, venue TEXT, interval TEXT,
        symbols TEXT, roi REAL, equity REAL, blob TEXT)""")
    con.commit()
    con.close()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_DB or "data/gridlab.db")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def save_run(result: dict, mode: str, venue: str) -> int:
    con = _conn()
    cur = con.execute(
        "INSERT INTO runs(ts,mode,venue,interval,symbols,roi,equity,blob) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), mode, venue, result.get("interval", ""),
         ",".join(i["sym"] for i in result.get("instruments", [])),
         result.get("roi", 0.0), result.get("equity", 0.0), json.dumps(result)))
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def get_run(run_id: int) -> Optional[dict]:
    con = _conn()
    row = con.execute("SELECT blob FROM runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def latest_run() -> Optional[dict]:
    con = _conn()
    row = con.execute("SELECT blob FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def list_runs(limit: int = 20) -> list[dict]:
    con = _conn()
    rows = con.execute(
        "SELECT id,ts,mode,venue,interval,symbols,roi,equity FROM runs ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "mode": r[2], "venue": r[3], "interval": r[4],
             "symbols": r[5], "roi": r[6], "equity": r[7]} for r in rows]


def export_csv(result: dict, kind: str = "log") -> str:
    """kind: 'log' (журнал событий) | 'trades' (только сделки)."""
    rows = result.get("log", [])
    if kind == "trades":
        rows = [r for r in rows if "Grid" in r["action"] or "Ликвид" in r["action"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "action", "symbol", "price", "size", "fee", "pnl", "comment"])
    for r in rows:
        w.writerow([r["ts"], r["action"], r["sym"], r["price"], r["size"],
                    r["fee"], r["pnl"], r["comment"]])
    return buf.getvalue()


def export_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


# ───────── экспорт ЖИВОЙ сессии (paper-аккаунт), а не бэктест-прогона ─────────
def live_events_csv(events, sym: str) -> str:
    """Полный журнал событий движка (StepEvent): старт/филлы/funding/ликвидации/стоп."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "time_msk", "action", "symbol", "price", "size", "fee", "pnl", "comment"])
    for e in events:
        w.writerow([e.ts, _msk(e.ts), e.action, sym, e.price, e.size,
                    round(e.fee, 6), round(e.pnl, 6), e.comment])
    return buf.getvalue()


def live_fills_csv(fills, sym: str) -> str:
    """Полный лог исполнений (Fill): каждая нога с ценой/объёмом/комиссией/мейкер-флагом."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "time_msk", "order_id", "symbol", "side", "price", "size",
                "fee", "is_maker", "note"])
    for f in fills:
        w.writerow([f.ts, _msk(f.ts), f.order_id, sym, f.side, f.price, f.size,
                    round(f.fee, 6), int(f.is_maker), f.note])
    return buf.getvalue()


def live_bundle_json(meta: dict, summary: dict, mm: dict, events, fills, sym: str,
                     equity_curve, balance_curve) -> str:
    """Полный снимок живой сессии одним JSON: мета+итоги+MM-метрики+события+филлы+кривые."""
    bundle = {
        "meta": meta,
        "summary": summary,
        "mm_metrics": mm,
        "equity_curve": equity_curve,
        "balance_curve": balance_curve,
        "events": [{"ts": e.ts, "time_msk": _msk(e.ts), "action": e.action, "symbol": sym,
                    "price": e.price, "size": e.size, "fee": round(e.fee, 6),
                    "pnl": round(e.pnl, 6), "comment": e.comment} for e in events],
        "fills": [{"ts": f.ts, "time_msk": _msk(f.ts), "order_id": f.order_id, "symbol": sym,
                   "side": f.side, "price": f.price, "size": f.size, "fee": round(f.fee, 6),
                   "is_maker": f.is_maker, "note": f.note} for f in fills],
    }
    return json.dumps(bundle, ensure_ascii=False, indent=1)
