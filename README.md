# JFT R3 / S3 F&O Scanner

Automated 5-minute scanner for NSE F&O stocks using Groww market-data APIs.

## Strategy

JFT levels are calculated from the previous completed daily candle:

```text
M = (PrevHigh + PrevLow) / 2
Range = PrevHigh - PrevLow

R3 = M + 0.96 × Range
R2 = M + 0.75 × Range
R1 = M + 0.29 × Range
S1 = M - 0.29 × Range
S2 = M - 0.75 × Range
S3 = M - 0.96 × Range
```

Signals:

- **BUY:** previous 5-minute scan price is below R3 and current price is at/above R3. Entry = current LTP. SL = R2.
- **SELL:** previous 5-minute scan price is above S3 and current price is at/below S3. Entry = current LTP. SL = S2.
- One open signal per symbol/side is kept in the dashboard.
- The scanner monitors NSE cash prices for every equity that has an NSE F&O future. It does not scan option contracts themselves.

## Groww API

The scanner uses Groww's instruments CSV to build the current F&O-stock universe, Groww historical candles for previous-day OHLC, and Groww LTP in batches of up to 50 symbols.

Set these GitHub Actions secrets:

### Recommended: TOTP authentication

```text
GROWW_TOTP_TOKEN
GROWW_TOTP_SECRET
```

### Or API key + secret

```text
GROWW_API_KEY
GROWW_API_SECRET
```

### Or a direct access token

```text
GROWW_ACCESS_TOKEN
```

The direct access token expires daily. Groww's API-key/secret approval flow can generate an access token at runtime; Groww also documents a TOTP flow.

## Dashboard connection

Set:

```text
DASHBOARD_URL=https://your-deployed-dashboard.example.com
SCANNER_SECRET=<random-long-secret>
```

Set the same `SCANNER_SECRET` on the dashboard service. If it is non-empty, scanner endpoints require the `X-Scanner-Secret` header.

## Schedule

`.github/workflows/jft-scanner.yml` runs every 5 minutes on weekdays using Asia/Kolkata timezone. The Python scanner ignores executions outside NSE market hours (09:15-15:30 IST).

GitHub Actions schedule has a minimum supported interval of 5 minutes, but scheduled runs can occasionally be delayed by GitHub. For production-grade tick-level triggering, a persistent Groww Feed/WebSocket service can replace the scheduled runner later.

## Important

This implementation generates signals and tracks them in the dashboard. It does **not** place live Groww orders. Broker order automation should be added only after paper-trading and validation.
