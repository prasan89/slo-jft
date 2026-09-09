import csv
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
GROWW_BASE = "https://api.groww.in"
INSTRUMENT_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
SCANNER_SECRET = os.environ.get("SCANNER_SECRET", "")
ACCESS_TOKEN = os.environ.get("GROWW_ACCESS_TOKEN", "")
API_KEY = os.environ.get("GROWW_API_KEY", "")
API_SECRET = os.environ.get("GROWW_API_SECRET", "")
TOTP_TOKEN = os.environ.get("GROWW_TOTP_TOKEN", "")
TOTP_SECRET = os.environ.get("GROWW_TOTP_SECRET", "")

# Reverse-engineered JFT coefficients confirmed against TradingView values.
# JFT levels are symmetric around the previous completed session midpoint:
#   R3/S3 = midpoint +/- 1.00 * range
#   R2/S2 = midpoint +/- 0.75 * range
#   R1/S1 = midpoint +/- 0.29 * range
OUTER = 1.00
MIDDLE = 0.75
INNER = 0.29
BATCH_SIZE = 50
WORKERS = 8

LEVELS_CACHE_DATE = None
LEVELS_CACHE = {}


def headers(token):
    return {"Accept": "application/json", "Authorization": f"Bearer {token}", "X-API-VERSION": "1.0"}


def get_access_token():
    if ACCESS_TOKEN:
        return ACCESS_TOKEN
    if API_KEY and API_SECRET:
        ts = str(int(time.time()))
        checksum = hashlib.sha256((API_SECRET + ts).encode()).hexdigest()
        r = requests.post(
            f"{GROWW_BASE}/v1/token/api/access",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={"key_type": "approval", "checksum": checksum, "timestamp": ts},
            timeout=20,
        )
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise RuntimeError(f"Groww auth failed: {r.text}")
        return token
    if TOTP_TOKEN and TOTP_SECRET:
        import pyotp
        totp = pyotp.TOTP(TOTP_SECRET).now()
        r = requests.post(
            f"{GROWW_BASE}/v1/token/api/access",
            headers={"Authorization": f"Bearer {TOTP_TOKEN}", "Content-Type": "application/json"},
            json={"key_type": "totp", "totp": totp},
            timeout=20,
        )
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise RuntimeError(f"Groww TOTP auth failed: {r.text}")
        return token
    raise RuntimeError("Set GROWW_ACCESS_TOKEN or Groww API key/secret or TOTP credentials")


def groww_get(path, params):
    token = get_access_token()
    gh = headers(token)
    for attempt in range(4):
        r = requests.get(f"{GROWW_BASE}{path}", headers=gh, params=params, timeout=30)
        if r.status_code == 401 and (API_KEY and API_SECRET or TOTP_TOKEN and TOTP_SECRET):
            token = get_access_token()
            gh = headers(token)
            r = requests.get(f"{GROWW_BASE}{path}", headers=gh, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"Groww API failure: {data}")
        return data["payload"]
    raise RuntimeError(f"Groww rate limit persisted: {path}")


def download_instruments():
    r = requests.get(INSTRUMENT_URL, timeout=60)
    r.raise_for_status()
    return list(csv.DictReader(r.content.decode("utf-8-sig").splitlines()))


def fno_stock_universe():
    rows = download_instruments()
    cash_symbols = {
        x["trading_symbol"].strip()
        for x in rows
        if x.get("exchange") == "NSE"
        and x.get("segment") == "CASH"
        and (x.get("series") or "").strip() == "EQ"
    }
    fno_underlyings = {
        (x.get("underlying_symbol") or "").strip()
        for x in rows
        if x.get("exchange") == "NSE"
        and x.get("segment") == "FNO"
        and (x.get("instrument_type") or "").strip().upper() == "FUT"
    }
    universe = sorted(x for x in fno_underlyings if x and x in cash_symbols)
    if not universe:
        raise RuntimeError("Could not build F&O equity universe from Groww instruments CSV")
    return universe


def previous_trading_day(now):
    day = now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def candle_local_date(candle):
    """Return the candle's date in India time for both Groww timestamp formats."""
    raw = candle[0]
    if isinstance(raw, (int, float)):
        value = float(raw)
        # Be defensive if an API response ever returns epoch milliseconds.
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, IST).date()
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt.date()


def daily_levels(symbol, trade_date, prev_date):
    start = f"{trade_date - timedelta(days=10)} 00:00:00"
    # Groww's historical endpoint can return the boundary/current daily candle
    # even when end_time is midnight. Therefore we explicitly filter by the
    # candle's India-local trading date instead of blindly taking max(timestamp).
    end = f"{trade_date} 23:59:59"
    payload = groww_get(
        "/v1/historical/candles",
        {
            "exchange": "NSE",
            "segment": "CASH",
            "groww_symbol": f"NSE-{symbol}",
            "start_time": start,
            "end_time": end,
            "candle_interval": "1day",
        },
    )
    candles = payload.get("candles", [])
    if not candles:
        raise RuntimeError(f"No daily candle before {trade_date} for {symbol}")

    # JFT must use YESTERDAY / the previous completed trading session, never
    # today's partially formed candle. This is the critical fix for the
    # dashboard's incorrect Prev High / Prev Low values.
    completed = [c for c in candles if candle_local_date(c) < trade_date]
    if not completed:
        raise RuntimeError(f"No completed daily candle before {trade_date} for {symbol}")

    candle = max(completed, key=lambda c: candle_local_date(c))
    candle_date = candle_local_date(candle)
    if candle_date != prev_date:
        print(f"LEVEL WARNING {symbol}: latest completed candle={candle_date}, expected={prev_date}")

    high, low = float(candle[2]), float(candle[3])
    midpoint = (high + low) / 2.0
    rng = high - low
    return {
        "trade_date": str(trade_date),
        "symbol": symbol,
        "prev_high": high,
        "prev_low": low,
        "midpoint": midpoint,
        "r3": midpoint + OUTER * rng,
        "r2": midpoint + MIDDLE * rng,
        "r1": midpoint + INNER * rng,
        "s1": midpoint - INNER * rng,
        "s2": midpoint - MIDDLE * rng,
        "s3": midpoint - OUTER * rng,
    }


def dashboard_get(path, params=None):
    if not DASHBOARD_URL:
        return None
    r = requests.get(f"{DASHBOARD_URL}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def dashboard_post(path, payload):
    if not DASHBOARD_URL:
        return None
    r = requests.post(
        f"{DASHBOARD_URL}{path}",
        headers={"Content-Type": "application/json", "X-Scanner-Secret": SCANNER_SECRET},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def load_or_build_levels(universe, trade_date, prev_date):
    global LEVELS_CACHE_DATE, LEVELS_CACHE
    cache_key = str(trade_date)
    if LEVELS_CACHE_DATE == cache_key and LEVELS_CACHE:
        return LEVELS_CACHE

    print(f"Building {len(universe)} JFT level sets from previous completed daily candles...")
    by_symbol = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(daily_levels, s, trade_date, prev_date): s for s in universe}
        for fut in as_completed(futures):
            symbol = futures[fut]
            try:
                by_symbol[symbol] = fut.result()
            except Exception as e:
                print(f"LEVEL ERROR {symbol}: {e}")

    if DASHBOARD_URL and by_symbol:
        dashboard_post("/api/jft/levels", {"levels": list(by_symbol.values())})

    LEVELS_CACHE_DATE = cache_key
    LEVELS_CACHE = {s: by_symbol[s] for s in universe if s in by_symbol}
    return LEVELS_CACHE


def batched(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_ltps(symbols):
    out = {}
    for batch in batched(symbols, BATCH_SIZE):
        payload = groww_get(
            "/v1/live-data/ltp",
            {"segment": "CASH", "exchange_symbols": ",".join(f"NSE_{s}" for s in batch)},
        )
        for key, value in payload.items():
            out[key.removeprefix("NSE_")] = float(value)
    return out


def send_signal(level, side, entry):
    payload = {
        "symbol": level["symbol"],
        "signal": side,
        "entry": entry,
        "ltp": entry,
        "r1": level["r1"],
        "r2": level["r2"],
        "r3": level["r3"],
        "s1": level["s1"],
        "s2": level["s2"],
        "s3": level["s3"],
        "timeframe": "5m",
        "time": datetime.now(IST).isoformat(),
    }
    if DASHBOARD_URL:
        print(
            f"{side} {level['symbol']} entry={entry:.2f} "
            f"SL={level['r2' if side == 'BUY' else 's2']:.2f} -> "
            f"{dashboard_post('/api/scanner/signal', payload)}"
        )
    else:
        print(f"{side} {level['symbol']} entry={entry:.2f} SL={level['r2' if side == 'BUY' else 's2']:.2f}")


def run():
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (dtime(9, 15) <= now.time() <= dtime(15, 30)):
        print(f"Outside NSE session: {now.isoformat()}")
        return

    trade_date, prev_date = now.date(), previous_trading_day(now)
    universe = fno_stock_universe()
    levels = load_or_build_levels(universe, trade_date, prev_date)
    ltps = get_ltps(list(levels))
    previous = (dashboard_get("/api/scanner/state", {"trade_date": str(trade_date)}) or {}) if DASHBOARD_URL else {}

    signals = 0
    for symbol, ltp in ltps.items():
        level = levels.get(symbol)
        if not level or symbol not in previous:
            continue
        prev_ltp = float(previous[symbol])
        if prev_ltp < level["r3"] <= ltp:
            send_signal(level, "BUY", ltp)
            signals += 1
        elif prev_ltp > level["s3"] >= ltp:
            send_signal(level, "SELL", ltp)
            signals += 1

    if DASHBOARD_URL:
        dashboard_post("/api/scanner/ltps", {"trade_date": str(trade_date), "ltps": ltps})
    print(f"JFT scan complete: universe={len(universe)} levels={len(levels)} ltps={len(ltps)} signals={signals}")


if __name__ == "__main__":
    run()
