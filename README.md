# Sonic bot

A trading server and a phone app. Only paper (simulated money at real prices) and Binance
demo trading exist; the signed client refuses every URL except the demo one.

## Download the app

Latest APK, built and published by CI on every push to `main` (like the Nokhba app):
https://github.com/yqwba0052-ship-it/sonic-bot/releases/latest/download/sonic.apk

## Strategy (from `research/`)

`trend-20-100-4h`: 20/100 EMA cross on 4-hour bars, buys only while BTC is above its 300-bar EMA
and sells only while below, stop at 2.5 x ATR(14), half closed at +0.25R with the stop then moved
to the entry, rest exits when the EMAs cross back or after 40 days (`research/sonic_research/improve.py`).

Tested 2021-01..2026-10 on 12 coins with fees, slippage and funding
(`research/reports/phase2.json`). Results:

- It was profitable in every year.
- It wins about 3 trades in 10, so long losing runs are normal.
- At 1% risk per trade, the deepest drop was 23-33%.

The bot's decisions match the backtest bar for bar (`research/tests/test_bot_matches_research.py`).

## Paper-trading replay of the real engine

`python -m sonic_research.engine_replay START END` (in `research/`) runs the server's own engine,
paper exchange, sizing and limits hour by hour on past Binance prices.

| Settings | Period | Trades | Winning | $30 became | Deepest drop | Engine errors |
|---|---|---|---|---|---|---|
| stop 3 ATR, 20 days (old) | 2021-06..2026-10 | 247 | 77.7% | $56.53 | 12.3% | 0 |
| **stop 2.5 ATR, 40 days (live)** | 2021-06..2026-10 | 266 | 80.8% | $86.72 | 15.0% | 0 |
| live | 2022 (bear market) | 22 | 95.5% | $31.72 | 11.0% | 0 |
| live | 2025-07..2026-10 | 59 | 74.6% | $33.98 | 8.9% | 0 |

Past results are no promise.

## Candle simulator

The app's candle tab replays the bot's trades candle by candle (play, pause, speed, next
event) for the bot's own trades now, with the live forming candle, or for the five-year
engine replay by half-year. See STRATEGY.md.

## Safety

- **Amount:** a hard total-loss budget. Losing it closes everything and halts the bot.
- **Daily loss limit:** reaching it halts the bot for the rest of the day.
- **Open trades:** at most `max_open` (default 3) at the same time.
- **Coins:** coins whose smallest Binance order needs more than the amount are refused.
- **Drift alarm:** if the average of the last 30 trades falls below -0.80R (worse than any period
  in 6 years of history), new entries stop until the owner looks.
- **Restarts:** the bot never resumes by itself after a restart; press Start in the app.
- **Smallest amount:** 30 USDT is the smallest tested amount that kept working
  (`research/reports/capital.json`).

## Run

1. Double-click `server/start_server.cmd`. The window shows the address and the access token.
2. Once, run `server/allow_phone_access.cmd`, so the phone on the home Wi-Fi can connect (asks for
   administrator approval). `allow_phone_access_off.cmd` undoes it.
3. Optional: `server/autostart_on.cmd` starts the server at Windows sign-in.
4. Phone: open `http://LAPTOP-IP:8787/sonic.apk` to install the app, or use the same address in
   the browser. Then enter the token in the Connection tab.

Paper trades are checked against 1-hour bars after the laptop wakes, so a stop touched during
sleep still counts. The bot can only act while the laptop is on.

## Optional: run 24 hours a day on a cloud server

1. Create an Ubuntu 24.04 server yourself, for example DigitalOcean 1 GB at about $6 a month.
   Pick a region that can reach Binance (not the USA), such as Frankfurt or Singapore.
2. In its console, as root:

   ```
   curl -fsSL https://raw.githubusercontent.com/yqwba0052-ship-it/sonic-bot/main/deploy/install_cloud.sh | bash
   ```

3. Put the printed `https://...sslip.io` address and token in the app under Connection, then Cloud.
   The app keeps both the laptop and the cloud connection, and you switch between them with one tap.

The script installs the bot as a restricted service and puts Caddy in front with automatic HTTPS,
so the token is never sent in clear text. The firewall allows only SSH and web ports. Running the
script again updates the bot and keeps its data. The laptop option "keep awake while the bot runs"
is in the app's Connection tab.

## Modes (`server/.env`)

- `SONIC_MODE=paper` is the default.
- `SONIC_MODE=demo` needs `BINANCE_DEMO_KEY` and `BINANCE_DEMO_SECRET`, from a Binance demo account
  in one-way position mode.

## Checks

```
cd server   && .venv\Scripts\python -m ruff check . && .venv\Scripts\python -m pytest -q
cd research && ..\server\.venv\Scripts\python -m pytest -q
cd app      && npx tsc --noEmit && npx eslint src
```

CI (`.github/workflows/build.yml`) runs the server tests and builds the signed APK on Linux.
