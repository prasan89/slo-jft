import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, desc
from db import session, Signal, JFTLevel, ScannerState

SCANNER_SECRET = os.getenv('SCANNER_SECRET','')
app = FastAPI(title='JFT R3/S3 Scanner', version='2.0')


def check_secret(request: Request):
    if SCANNER_SECRET and request.headers.get('X-Scanner-Secret') != SCANNER_SECRET:
        raise HTTPException(401,'Invalid scanner secret')


def as_dict(obj):
    return {c.name:getattr(obj,c.name) for c in obj.__table__.columns}


@app.get('/health')
def health(): return {'status':'UP','service':'jft-dashboard','time':datetime.now(timezone.utc).isoformat()}

@app.get('/', response_class=HTMLResponse)
def dashboard():
    with open('static/index.html','r',encoding='utf-8') as f: return f.read()

@app.get('/api/signals')
def signals():
    with session() as db:
        rows=db.scalars(select(Signal).order_by(desc(Signal.id)).limit(500)).all()
        return [as_dict(x) for x in rows]

@app.get('/api/stats')
def stats():
    with session() as db:
        total=db.scalar(select(func.count()).select_from(Signal)) or 0
        open_count=db.scalar(select(func.count()).select_from(Signal).where(Signal.status=='OPEN')) or 0
        closed=db.scalar(select(func.count()).select_from(Signal).where(Signal.status=='CLOSED')) or 0
        pnl=db.scalar(select(func.coalesce(func.sum(Signal.pnl),0)).select_from(Signal)) or 0
        wins=db.scalar(select(func.count()).select_from(Signal).where(Signal.status=='CLOSED',Signal.pnl>0)) or 0
        return {'total':total,'open':open_count,'closed':closed,'pnl':float(pnl),'win_rate':wins/closed*100 if closed else 0}

@app.get('/api/jft/levels')
def get_levels(trade_date:str):
    with session() as db:
        rows=db.scalars(select(JFTLevel).where(JFTLevel.trade_date==trade_date).order_by(JFTLevel.symbol)).all()
        return [as_dict(x) for x in rows]

@app.post('/api/jft/levels')
async def upsert_levels(request:Request):
    check_secret(request); data=await request.json(); levels=data.get('levels',[])
    with session() as db:
        for x in levels:
            row=db.scalar(select(JFTLevel).where(JFTLevel.trade_date==x['trade_date'],JFTLevel.symbol==x['symbol']))
            if not row: row=JFTLevel(trade_date=x['trade_date'],symbol=x['symbol']); db.add(row)
            for k in ('prev_high','prev_low','midpoint','r1','r2','r3','s1','s2','s3'): setattr(row,k,float(x[k]))
        db.commit()
    return {'ok':True,'count':len(levels)}

@app.get('/api/scanner/state')
def scanner_state(trade_date:str):
    with session() as db:
        rows=db.scalars(select(ScannerState).where(ScannerState.trade_date==trade_date)).all()
        return {x.symbol:x.ltp for x in rows}

@app.post('/api/scanner/ltps')
async def scanner_ltps(request:Request):
    check_secret(request); data=await request.json(); trade_date=str(data['trade_date']); ltps=data.get('ltps',{}); now=datetime.now(timezone.utc).isoformat(); updated=0
    with session() as db:
        for symbol,value in ltps.items():
            row=db.scalar(select(ScannerState).where(ScannerState.trade_date==trade_date,ScannerState.symbol==symbol))
            if not row: row=ScannerState(trade_date=trade_date,symbol=symbol,ltp=float(value),updated_at=now); db.add(row)
            else: row.ltp=float(value); row.updated_at=now
            open_signals=db.scalars(select(Signal).where(Signal.symbol==symbol,Signal.status=='OPEN')).all()
            for sig in open_signals:
                sig.ltp=float(value); sig.pnl=(float(value)-sig.entry) if sig.side=='BUY' else (sig.entry-float(value)); sig.pnl_pct=sig.pnl/sig.entry*100; updated+=1
        db.commit()
    return {'ok':True,'symbols':len(ltps),'updated_signals':updated}

@app.post('/api/scanner/signal')
async def scanner_signal(request:Request):
    check_secret(request); data=await request.json(); side=str(data.get('signal',data.get('side',''))).upper()
    if side not in ('BUY','SELL'): raise HTTPException(400,'signal must be BUY or SELL')
    required=('symbol','entry','r1','r2','r3','s1','s2','s3')
    if any(k not in data for k in required): raise HTTPException(400,'Missing scanner signal fields')
    symbol=str(data['symbol']); entry=float(data['entry'])
    with session() as db:
        existing=db.scalar(select(Signal).where(Signal.symbol==symbol,Signal.side==side,Signal.status=='OPEN').limit(1))
        if existing: return {'ok':True,'duplicate':True,'id':existing.id}
        sig=Signal(symbol=symbol,side=side,entry=entry,stop_loss=float(data['r2'] if side=='BUY' else data['s2']),r1=float(data['r1']),r2=float(data['r2']),r3=float(data['r3']),s1=float(data['s1']),s2=float(data['s2']),s3=float(data['s3']),ltp=float(data.get('ltp',entry)),pnl=0,pnl_pct=0,status='OPEN',timeframe=str(data.get('timeframe','5m')),signal_time=str(data.get('time',datetime.now(timezone.utc).isoformat())),created_at=datetime.now(timezone.utc).isoformat())
        db.add(sig); db.commit(); db.refresh(sig)
        return {'ok':True,'id':sig.id,'side':side,'entry':entry,'stop_loss':sig.stop_loss}

@app.post('/api/signals/{signal_id}/close')
async def close_signal(signal_id:int,request:Request):
    check_secret(request); data=await request.json(); ltp=float(data['ltp'])
    with session() as db:
        sig=db.get(Signal,signal_id)
        if not sig: raise HTTPException(404,'Signal not found')
        sig.ltp=ltp; sig.pnl=(ltp-sig.entry) if sig.side=='BUY' else (sig.entry-ltp); sig.pnl_pct=sig.pnl/sig.entry*100; sig.status='CLOSED'; db.commit(); return {'ok':True,'pnl':sig.pnl,'pnl_pct':sig.pnl_pct}

@app.post('/webhook/tradingview')
async def tradingview_webhook(request:Request):
    data=await request.json(); side=str(data.get('signal',data.get('side',''))).upper()
    if side not in ('BUY','SELL'): raise HTTPException(400,'signal must be BUY or SELL')
    try:
        symbol=str(data['symbol']); entry=float(data.get('entry',data.get('price',data['close']))); r1=float(data['r1']); r2=float(data['r2']); s1=float(data['s1']); s2=float(data['s2'])
    except (KeyError,TypeError,ValueError): raise HTTPException(400,'Required fields: symbol, signal, price/entry, r1, r2, s1, s2')
    with session() as db:
        existing=db.scalar(select(Signal).where(Signal.symbol==symbol,Signal.side==side,Signal.status=='OPEN').limit(1))
        if existing: return {'ok':True,'duplicate':True,'id':existing.id}
        sig=Signal(symbol=symbol,side=side,entry=entry,stop_loss=r1 if side=='BUY' else s1,r1=r1,r2=r2,r3=None,s1=s1,s2=s2,s3=None,ltp=float(data.get('ltp',entry)),pnl=0,pnl_pct=0,status='OPEN',timeframe=str(data.get('timeframe',data.get('interval',''))),signal_time=str(data.get('time',datetime.now(timezone.utc).isoformat())),created_at=datetime.now(timezone.utc).isoformat()); db.add(sig); db.commit(); db.refresh(sig); return {'ok':True,'id':sig.id}

app.mount('/static',StaticFiles(directory='static'),name='static')
