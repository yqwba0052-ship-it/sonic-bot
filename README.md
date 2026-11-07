# Sonic bot

First version: a trading server and a phone app. Only simulated and Binance demo trading exist.
The strategy is a system test (`demo-ema-9-21`), not a proven profitable strategy.

## Run

1. Double-click `server/start_server.cmd`. Allow Python on private networks if Windows asks.
2. The window prints the server address and the access token.
3. Put the phone on the same Wi-Fi and open that address in the browser.
4. In the Connection tab, enter the token.

## Modes

Set in `server/.env`:

- `SONIC_MODE=paper`: real public prices, simulated money, fees and slippage. This is the default.
- `SONIC_MODE=demo`: Binance demo futures. Also needs `BINANCE_DEMO_KEY` and `BINANCE_DEMO_SECRET`.
  The demo account must use one-way position mode.

Real-money trading is not implemented. The signed client refuses any URL except the demo URL.

## Checks

```
cd server
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pytest -q -m live -o addopts=""
cd ..\app
npx tsc --noEmit
npx eslint src
npx expo export --platform web --output-dir ../server/web
```

## Android APK

A local Windows build fails on the Windows path limit in React Native C++ code, even from `C:\sb`.
Build on Linux, such as GitHub Actions or EAS.
