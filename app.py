import os
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.getenv("DB_PATH", "signals.db")
app = FastAPI(title="JFT Signal Dashboard")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            r1 REAL,
            r2 REAL,
            s1 REAL,
            s2 REAL,
            ltp REAL,
            pnl REAL,
            pnl_pct REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            timeframe TEXT,
            signal_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/signals")
def signals():
    conn = db()
    rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
    open_count = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='OPEN'").fetchone()["c"]
    closed = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='CLOSED'").fetchone()["c"]
    pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) p FROM signals").fetchone()["p"]
    wins = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='CLOSED' AND pnl > 0").fetchone()["c"]
    conn.close()
    win_rate = (wins / closed * 100) if closed else 0
    return {"total": total, "open": open_count, "closed": closed, "pnl": pnl, "win_rate": win_rate}


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Webhook body must be JSON")

    side = str(data.get("signal", data.get("side", ""))).upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(400, "signal must be BUY or SELL")

    try:
        entry = float(data.get("entry", data.get("price", data.get("close"))))
        r1 = float(data["r1"])
        r2 = float(data["r2"])
        s1 = float(data["s1"])
        s2 = float(data["s2"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Required fields: symbol, signal, price/entry, r1, r2, s1, s2")

    # BUY crossing R2 -> protective SL is R1.
    # SELL crossing S2 -> protective SL is S1.
    stop_loss = r1 if side == "BUY" else s1
    symbol = str(data.get("symbol", "UNKNOWN"))
    ltp = float(data.get("ltp", entry))
    signal_time = str(data.get("time", datetime.now(timezone.utc).isoformat()))
    timeframe = str(data.get("timeframe", data.get("interval", "")))

    conn = db()
    # Keep only one open signal per symbol/side to avoid duplicate alerts.
    existing = conn.execute(
        "SELECT id FROM signals WHERE symbol=? AND side=? AND status='OPEN' LIMIT 1",
        (symbol, side),
    ).fetchone()
    if existing:
        conn.close()
        return {"ok": True, "duplicate": True, "id": existing["id"]}

    cur = conn.execute(
        """INSERT INTO signals
        (symbol,side,entry,stop_loss,r1,r2,s1,s2,ltp,pnl,pnl_pct,status,timeframe,signal_time,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,0,0,'OPEN',?,?,?)""",
        (symbol, side, entry, stop_loss, r1, r2, s1, s2, ltp, timeframe,
         signal_time, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return {"ok": True, "id": signal_id, "side": side, "entry": entry, "stop_loss": stop_loss}


@app.post("/api/ltp")
async def update_ltp(request: Request):
    data = await request.json()
    symbol = str(data.get("symbol", ""))
    ltp = float(data["ltp"])
    conn = db()
    rows = conn.execute("SELECT * FROM signals WHERE symbol=? AND status='OPEN'", (symbol,)).fetchall()
    for r in rows:
        pnl = (ltp - r["entry"]) if r["side"] == "BUY" else (r["entry"] - ltp)
        pnl_pct = pnl / r["entry"] * 100
        conn.execute("UPDATE signals SET ltp=?, pnl=?, pnl_pct=? WHERE id=?", (ltp, pnl, pnl_pct, r["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "updated": len(rows)}


@app.post("/api/signals/{signal_id}/close")
async def close_signal(signal_id: int, request: Request):
    data = await request.json()
    ltp = float(data["ltp"])
    conn = db()
    r = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "Signal not found")
    pnl = (ltp - r["entry"]) if r["side"] == "BUY" else (r["entry"] - ltp)
    pnl_pct = pnl / r["entry"] * 100
    conn.execute("UPDATE signals SET ltp=?, pnl=?, pnl_pct=?, status='CLOSED' WHERE id=?", (ltp, pnl, pnl_pct, signal_id))
    conn.commit()
    conn.close()
    return {"ok": True, "pnl": pnl, "pnl_pct": pnl_pct}


app.mount("/static", StaticFiles(directory="static"), name="static")
