# Sonic bot

A trading server and a phone app. Only paper (simulated money at real prices) and Binance
demo trading exist; the signed client refuses every URL except the demo one.

## Download the app

Latest APK, built and published by CI on every push to `main` (like the Nokhba app):
https://github.com/yqwba0052-ship-it/sonic-bot/releases/latest/download/sonic.apk

## Strategy (from `research/`)

`trend-20-100-4h`: 20/100 EMA cross on 4-hour bars, buys only while BTC is above its 300-bar EMA
and the coin above its own 200-bar EMA (sells only in the mirror case), stop at 2.5 x ATR(14),
half closed at +0.20R with the stop then moved
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
| stop 2.5 ATR, 40 days, half at 0.25R | 2021-06..2026-10 | 266 | 80.8% | $86.72 | 15.0% | 0 |
| **+ coin above EMA 200, half at 0.20R (live)** | 2021-06..2026-10 | 224 | 82.6% | $83.49 | 15.0% | 0 |

The owner's goal is 80-90% winning trades each month. With the live settings, 67% of the months
with 3 or more trades reached 80% (was 56%). See STRATEGY.md.

Past results are no promise.

## Candle simulator

The app's candle tab replays the bot's trades candle by candle (play, pause, speed, next
event) for the bot's own trades now, with the live forming candle, or for the five-year
engine replay by half-year. See STRATEGY.md.

## Learning from traders (paper only)

The app's "Traders" tab (المتداولون): add any number of traders by name, Binance link or lead
trader id, import their closed trades, and Sonic learns an independent rule set per trader,
tests it on the later 30% of the time span (out of sample) next to Sonic's own rules in the
same simulator ($30, 1% risk, fees and slippage), and paper-trades it in its own table.
Pause stops a trader's paper trading; delete asks "are you sure?" and then takes the trader
off the list and stops watching it, while its trades, strategies and test results stay saved.

Strategies are compared per coin (`GET /strategies`): the latest learned version of every
trader's strategy, deleted traders included, is run again on each of its coins, out of sample.
When two or more trade the same coin they are compared over the same period (the overlap of
their test periods); a coin with one strategy is judged on its own. Score: profit after fees per
year, profit factor, profitable months, deepest drawdown, worst single trade and win rate.
Too little data (under 10 trades, 60 days or 3 months on that coin), losses, drawdowns over 20%,
single losses over 3%, profit factor under 1.2 or under half the months profitable leave a
strategy out, with the reason. The comparison reruns after every study and daily; the best per
coin is paper-traded on that coin. A better strategy never replaces one the owner approved or
changes its money without the owner. Nothing here ever places an order or changes Sonic's
strategy.

Data access (checked 2026-11-11): Binance's official copy-trading API only serves the caller's
own lead-trader account; there is no official endpoint for another trader's positions or for
which lead trader a copied trade came from. Trades therefore come by CSV (`GET
/traders/template.csv`, or fills paired into positions), or from the owner's own futures history
through a key that Binance reports as read-only (any trading, transfer or withdrawal permission
is refused; the client can only GET three fixed paths). Code: `server/sonic/leader.py`,
`server/sonic/account.py`.

Check on real prices: Sonic's own 224 replay trades imported as a "trader". The lab found
"buy with the trend while BTC is up, at the first bar after the cross" by itself; out of sample
(543 days) the learned rules made 65 trades, 78% wins, +$1.02, 5% drawdown, against Sonic's
65 trades, 77%, +$4.72, 3% in the same simulator. A learned rule set is a guess at patterns in
a trader's results, not the trader's real strategy.

Money for approved strategies (`server/sonic/capital.py`), three separate steps:
1. approve (the check mark): only a strategy that passed out of sample on a coin; no money, no
   trading;
2. allocate an amount in USDT: the futures balance is read with the read-only key; the app shows
   the minimum (Binance's smallest order on every coin of the strategy at 1% risk) and the
   maximum (free balance minus other strategies' amounts, at most half the wallet for one
   strategy and 80% for all), and says "الرصيد غير كافٍ" or "لا يوجد رصيد متاح". Reservations are
   Sonic's own bookkeeping: Binance does not isolate money per strategy inside one account;
3. activate, only with: a successful paper test (20+ trades, profit after fees, profit factor
   1.2+, drawdown 20% or less), the owner's typed approval, a futures-only trading key (no
   withdrawal, no transfer), total and daily loss limits, and the emergency stop. Target:
   Binance Demo; real money also needs `SONIC_LIVE_TRADING=yes` in the server's `.env`, which
   only the owner sets. A strategy trades only its trader's coins, never beyond its amount,
   never a coin used by another live strategy or holding a position Sonic did not open. Stop
   keeps open trades with their stop and target, or closes them at market. The order client
   was tested with simulated Binance replies only: run on Binance Demo first.

Read-only Binance key, two safe places (never in chat, never in git):
- the app: Traders tab, "ربط حسابك في Binance" (stored on the server in sonic.db, readable by
  the bot's user only, never shown again);
- or the server's `server/.env` (git-ignored): `BINANCE_READ_KEY=...` and
  `BINANCE_READ_SECRET=...`, then `python -m sonic.check_account` (Windows: `check_account.cmd`)
  for a read-only connection test that places no order, and restart the server. A key in
  `.env` takes precedence over one saved from the app.
Either way the key is refused unless Binance reports it as read-only, it is re-checked before
every use, and the account is synced every 6 hours.

CSV import never invents data. Every row is reported as saved, draft, still open or error, with
the fields it lacks. A row with prices but no open/close time is kept as a draft: it is never
used for learning, ranking or paper trading until the real times are added in the app or the
same trades are imported again with their times (the drafts are then replaced). A missing fee,
leverage, size or profit stays empty. Column names of Binance's position history (Opened,
Closed, Avg. Entry Price, Avg. Close Price, Closing PnL, Direction) and trade history (single
fills) are recognised.

Automatic import (no manual completion needed when the data exists):
- a Binance CSV file chosen in the app, or pasted text; a screenshot of the trader's position
  history read by OCR on the server (Tesseract, English and Arabic; the read text is shown for
  review). Coin, side, leverage, open and close times, prices, size, profit and fee are taken
  when present; anything not clearly there stays missing;
- times are converted to UTC: a file whose time column says UTC is kept as is, other times are
  the phone's time zone (sent by the app), a time with its own zone keeps it;
- a complete record of a position completes its draft automatically (same coin and side,
  prices within 0.05%); fields missing on one side are taken from the other real record; the
  same position from two sources is never saved twice;
- a trader's strategy is studied automatically in the background once it has 20 complete
  trades and new ones since the last study, then all coins are compared again.
- The owner's own account: the API keeps 3 months; months 4 to 6 come from Binance's official
  futures history file (requested once; 5 requests a month are allowed), so learning from the
  account covers 6 months.

How trades arrive, shown per trader in the app: a lead trader's trades are never collected
by themselves (Binance offers no official access to other traders' trades, and copy trades do
not appear in the owner's futures account through the API); they come only from what the owner
imports (file or screenshot), with the time of the last one. Only the owner's own account is
read automatically, every 6 hours, with the read-only key. "Active" means used for analysis
and paper trading, not that trades are being collected.

Kopyon (owner's choice, 2026-11-13; third-party, NOT an official Binance API, `server/sonic/kopyon.py`):
closed positions of the lead traders the owner added (by their Binance link/id), every 10
minutes, after a one-time history of up to 6 months. Only GET positionHistory with the owner's
Kopyon key (app Traders tab, or KOPYON_API_KEY in server/.env; separate from the Binance keys,
which never go to Kopyon). Each trade keeps source "kopyon" and Kopyon's positionId, so nothing
is saved twice; times are Binance's milliseconds; fees are not published and stay empty;
positions not fully closed are skipped until closed. Sonic counts its own requests and stays
under 9,500 per 30 days (free plan 10,000), widening the interval when more traders are watched.
The app shows the last sync, trades collected and connection errors per trader. Study starts
by itself at 20 complete trades with 3 losing ones, then after 5 more (or daily).

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
4. Press Start, and switch on "resume after server restart" (Connection tab) so a reboot of the
   server does not end a month-long run. The bot still stays stopped after a restart if you
   stopped it, or if the panic button or a loss limit halted it. Stop the laptop bot.

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
