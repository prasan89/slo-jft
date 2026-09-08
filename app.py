import os
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.getenv("DB_PATH", "signals.db")
SCANNER_SECRET = os.getenv("SCANNER_SECRET", "")
app = FastAPI(title="JFT Signal Dashboard")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL,
        entry REAL NOT NULL, stop_loss REAL NOT NULL, r1 REAL, r2 REAL, r3 REAL,
        s1 REAL, s2 REAL, s3 REAL, ltp REAL, pnl REAL, pnl_pct REAL,
        status TEXT NOT NULL DEFAULT 'OPEN', timeframe TEXT, signal_time TEXT NOT NULL, created_at TEXT NOT NULL)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    for col in ("r3", "s3"):
        if col not in cols:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS jft_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
        prev_high REAL NOT NULL, prev_low REAL NOT NULL, midpoint REAL NOT NULL,
        r1 REAL NOT NULL, r2 REAL NOT NULL, r3 REAL NOT NULL,
        s1 REAL NOT NULL, s2 REAL NOT NULL, s3 REAL NOT NULL,
        UNIQUE(trade_date, symbol))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scanner_state (
        trade_date TEXT NOT NULL, symbol TEXT NOT NULL, ltp REAL NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(trade_date, symbol))""")
    conn.commit(); conn.close()


init_db()


def check_scanner_secret(request: Request):
    if SCANNER_SECRET and request.headers.get("X-Scanner-Secret") != SCANNER_SECRET:
        raise HTTPException(401, "Invalid scanner secret")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/signals")
def signals():
    conn = db(); rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 500").fetchall(); conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
    open_count = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='OPEN'").fetchone()["c"]
    closed = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='CLOSED'").fetchone()["c"]
    pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) p FROM signals").fetchone()["p"]
    wins = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='CLOSED' AND pnl > 0").fetchone()["c"]
    conn.close(); return {"total": total, "open": open_count, "closed": closed, "pnl": pnl, "win_rate": (wins / closed * 100) if closed else 0}


@app.get("/api/jft/levels")
def get_levels(trade_date: str):
    conn = db(); rows = conn.execute("SELECT * FROM jft_levels WHERE trade_date=?", (trade_date,)).fetchall(); conn.close()
    return [dict(r) for r in rows]


@app.post("/api/jft/levels")
async def upsert_levels(request: Request):
    check_scanner_secret(request); data = await request.json(); levels = data.get("levels", [])
    conn = db()
    for x in levels:
        conn.execute("""INSERT INTO jft_levels
            (trade_date,symbol,prev_high,prev_low,midpoint,r1,r2,r3,s1,s2,s3)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date,symbol) DO UPDATE SET
            prev_high=excluded.prev_high,prev_low=excluded.prev_low,midpoint=excluded.midpoint,
            r1=excluded.r1,r2=excluded.r2,r3=excluded.r3,s1=excluded.s1,s2=excluded.s2,s3=excluded.s3""",
            (x["trade_date"],x["symbol"],x["prev_high"],x["prev_low"],x["midpoint"],x["r1"],x["r2"],x["r3"],x["s1"],x["s2"],x["s3"]))
    conn.commit(); conn.close(); return {"ok": True, "count": len(levels)}


@app.post("/api/scanner/ltps")
async def scanner_ltps(request: Request):
    check_scanner_secret(request); data = await request.json(); trade_date = str(data["trade_date"]); ltps = data.get("ltps", {})
    now = datetime.now(timezone.utc).isoformat(); conn = db(); updated = 0
    for symbol, value in ltps.items():
        ltp = float(value)
        conn.execute("""INSERT INTO scanner_state(trade_date,symbol,ltp,updated_at) VALUES (?,?,?,?)
            ON CONFLICT(trade_date,symbol) DO UPDATE SET ltp=excluded.ltp,updated_at=excluded.updated_at""", (trade_date,symbol,ltp,now))
        for r in conn.execute("SELECT * FROM signals WHERE symbol=? AND status='OPEN'", (symbol,)).fetchall():
            pnl = (ltp-r["entry"]) if r["side"] == "BUY" else (r["entry"]-ltp); pnl_pct = pnl/r["entry"]*100
            conn.execute("UPDATE signals SET ltp=?,pnl=?,pnl_pct=? WHERE id=?", (ltp,pnl,pnl_pct,r["id"])); updated += 1
    conn.commit(); conn.close(); return {"ok": True, "symbols": len(ltps), "updated_signals": updated}


@app.get("/api/scanner/state")
def scanner_state(trade_date: str):
    conn = db(); rows = conn.execute("SELECT symbol,ltp FROM scanner_state WHERE trade_date=?", (trade_date,)).fetchall(); conn.close()
    return {r["symbol"]: r["ltp"] for r in rows}


@app.post("/api/scanner/signal")
async def scanner_signal(request: Request):
    check_scanner_secret(request); data = await request.json(); side = str(data.get("signal", data.get("side", ""))).upper()
    if side not in {"BUY", "SELL"}: raise HTTPException(400, "signal must be BUY or SELL")
    required = ["symbol","entry","r1","r2","r3","s1","s2","s3"]
    if any(k not in data for k in required): raise HTTPException(400, "Missing scanner signal fields")
    symbol = str(data["symbol"]); entry = float(data["entry"]); r1,r2,r3 = float(data["r1"]),float(data["r2"]),float(data["r3"]); s1,s2,s3 = float(data["s1"]),float(data["s2"]),float(data["s3"])
    stop_loss = r2 if side == "BUY" else s2; ltp = float(data.get("ltp",entry)); signal_time = str(data.get("time",datetime.now(timezone.utc).isoformat())); timeframe = str(data.get("timeframe","5m"))
    conn = db(); existing = conn.execute("SELECT id FROM signals WHERE symbol=? AND side=? AND status='OPEN' LIMIT 1",(symbol,side)).fetchone()
    if existing: conn.close(); return {"ok":True,"duplicate":True,"id":existing["id"]}
    cur = conn.execute("""INSERT INTO signals
        (symbol,side,entry,stop_loss,r1,r2,r3,s1,s2,s3,ltp,pnl,pnl_pct,status,timeframe,signal_time,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,'OPEN',?,?,?)""",
        (symbol,side,entry,stop_loss,r1,r2,r3,s1,s2,s3,ltp,timeframe,signal_time,datetime.now(timezone.utc).isoformat()))
    conn.commit(); signal_id=cur.lastrowid; conn.close(); return {"ok":True,"id":signal_id,"side":side,"entry":entry,"stop_loss":stop_loss}


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    try: data = await request.json()
    except Exception: raise HTTPException(400,"Webhook body must be JSON")
    side=str(data.get("signal",data.get("side",""))).upper()
    if side not in {"BUY","SELL"}: raise HTTPException(400,"signal must be BUY or SELL")
    try:
        entry=float(data.get("entry",data.get("price",data.get("close")))); r1=float(data["r1"]); r2=float(data["r2"]); s1=float(data["s1"]); s2=float(data["s2"])
    except (KeyError,TypeError,ValueError): raise HTTPException(400,"Required fields: symbol, signal, price/entry, r1, r2, s1, s2")
    symbol=str(data.get("symbol","UNKNOWN")); ltp=float(data.get("ltp",entry)); signal_time=str(data.get("time",datetime.now(timezone.utc).isoformat())); timeframe=str(data.get("timeframe",data.get("interval",""))); stop_loss=r1 if side=="BUY" else s1
    conn=db(); existing=conn.execute("SELECT id FROM signals WHERE symbol=? AND side=? AND status='OPEN' LIMIT 1",(symbol,side)).fetchone()
    if existing: conn.close(); return {"ok":True,"duplicate":True,"id":existing["id"]}
    cur=conn.execute("""INSERT INTO signals
        (symbol,side,entry,stop_loss,r1,r2,r3,s1,s2,s3,ltp,pnl,pnl_pct,status,timeframe,signal_time,created_at)
        VALUES (?,?,?,?,?,?,NULL,?,?,NULL,?,0,0,'OPEN',?,?,?)""",(symbol,side,entry,stop_loss,r1,r2,s1,s2,ltp,timeframe,signal_time,datetime.now(timezone.utc).isoformat()))
    conn.commit(); signal_id=cur.lastrowid; conn.close(); return {"ok":True,"id":signal_id,"side":side,"entry":entry,"stop_loss":stop_loss}


@app.post("/api/ltp")
async def update_ltp(request: Request):
    data=await request.json(); symbol=str(data.get("symbol","")); ltp=float(data["ltp"]); conn=db(); rows=conn.execute("SELECT * FROM signals WHERE symbol=? AND status='OPEN'",(symbol,)).fetchall()
    for r in rows:
        pnl=(ltp-r["entry"]) if r["side"]=="BUY" else (r["entry"]-ltp); pnl_pct=pnl/r["entry"]*100; conn.execute("UPDATE signals SET ltp=?,pnl=?,pnl_pct=? WHERE id=?",(ltp,pnl,pnl_pct,r["id"]))
    conn.commit(); conn.close(); return {"ok":True,"updated":len(rows)}


@app.post("/api/signals/{signal_id}/close")
async def close_signal(signal_id:int,request:Request):
    data=await request.json(); ltp=float(data["ltp"]); conn=db(); r=conn.execute("SELECT * FROM signals WHERE id=?",(signal_id,)).fetchone()
    if not r: conn.close(); raise HTTPException(404,"Signal not found")
    pnl=(ltp-r["entry"]) if r["side"]=="BUY" else (r["entry"]-ltp); pnl_pct=pnl/r["entry"]*100; conn.execute("UPDATE signals SET ltp=?,pnl=?,pnl_pct=?,status='CLOSED' WHERE id=?",(ltp,pnl,pnl_pct,signal_id)); conn.commit(); conn.close(); return {"ok":True,"pnl":pnl,"pnl_pct":pnl_pct}


app.mount("/static", StaticFiles(directory="static"), name="static")
