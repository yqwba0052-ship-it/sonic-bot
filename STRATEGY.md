# Sonic trading method (live settings)

This is how the bot trades today, and why. Every number here was tested; see `research/`.

## Market and timing

- **Venue:** Binance USDT perpetual futures, isolated margin, one-way mode. Paper trading uses
  real public prices with simulated money; demo trading uses Binance's demo platform.
- **Coins:** SOL, XRP, DOGE, ADA, SUI, TRX, BNB, AVAX. These are liquid coins whose smallest
  order fits a small amount. Eight more coins were tested (NEAR, UNI, AAVE, FIL, XLM, DOT, ATOM,
  ZEC), and none passed on both the training and the unseen years.
- **Bars:** 4 hours. The bot checks once each time a 4-hour bar closes.

## Entry

1. The 20-bar EMA crosses above the 100-bar EMA on a closed bar.
2. BTC closes above its 300-bar EMA, which is about 50 days. When BTC is falling the bot does not
   buy. This filter kept the account flat in 2022.
3. The coin itself closes above its own 200-bar EMA (about 33 days). Added 2026-11-10: more
   months reach the owner's 80% goal (see "Strategy trials" below).
4. Longs only. Shorts lost money in 2024-2026 while longs won every year. The owner can allow
   shorts in the app.

Every trade stores why it opened: both EMA values, the BTC trend, ATR and the stop distance.

## Size and safety

- **Risk:** 1% of the remaining amount per trade. A trade below Binance's smallest order is
  raised to that minimum only if its stop then risks at most 2% of the amount; otherwise it is
  skipped.
- **Open trades:** at most 3. **Leverage:** 3, with a maximum of 5. Leverage sets the margin only;
  x24 is refused because Binance would liquidate the trade before its stop.
- **Amount:** a hard budget. Losing all of it closes everything and halts the bot.
- **Daily loss limit:** reaching it closes everything and halts the bot for the day.
- **Drift alarm:** the last 30 trades averaging below -0.25R stops new entries. In 5.4 years of
  history the worst 30-trade average was -0.17R.
- **Network outages:** the bot rides them out; it halts after 30 minutes without a break.

## Exit

1. **Stop:** 2.5 x ATR(14) below the entry, placed on the exchange.
2. **Half profit:** half the position closes at +0.20 x the stop distance. The stop on the rest
   then moves to the entry price, so most trades end in profit.
3. **The rest** exits when the EMAs cross back, or after 40 days.

## What history says

On the real engine, 2021-06..2026-10, a $30 account:

| | |
|---|---|
| Trades | 224 |
| Winning trades | 82.6% |
| Months (3+ trades) with 80%+ winning | 24 of 36 (67%) |
| End amount | $83.49 |
| Deepest drop | 15% |

Months that still fell under 60%: 2021-07, 2024-04, 2025-06, 2026-07, all broad market drops
where several coins hit their stops together. Past results are no promise.

## Strategy trials (2026-11-10)

Each idea ran on the real engine, 2021-06..2026-10, $30. Judged on 2021-2023 and, separately, on
the unseen 2024-2026. "Months 80%+" counts months with 3 or more closed trades.

| Idea | Months 80%+ 2021-23 | Months 80%+ 2024-26 | Winning 2024-26 | $30 became |
|---|---|---|---|---|
| Before (half at 0.25R) | 59% | 55% | 78.6% | $86.72 |
| Half at 0.20R | 71% | 61% | 79.4% | $87.46 |
| Half at 0.15R | 53% | 48% | 75.9% | $76.04 |
| Coin above EMA 300 | 75% | 72% | 83.7% | $64.79 |
| Coin above EMA 300 + half 0.20R | 75% | 72% | 84.4% | $62.88 |
| **Coin above EMA 200 + half 0.20R (chosen)** | 64% | 68% | 81.2% | $83.49 |
| Strong BTC (EMA 50 above EMA 300) | 50% | 68% | 81.1% | $83.75 |
| Strong BTC + half 0.20R | 60% | 70% | 82.7% | $81.22 |
| Skip stops wider than 10% | 65% | 52% | 78.4% | $88.42 |
| Half 0.20R + 3-day pause after a loss | 71% | 52% | 78.6% | $84.47 |
| Coin EMA 300 + half 0.20R + pause | 75% | 68% | 82.9% | $61.47 |

The 3-day pause after a loss did not help in any combination.

The coin EMA-300 filter gives the steadiest months but cut 2021-2023 profit from $14 to $2.
EMA 200 keeps almost all the profit and still improves both periods. Half at 0.15R wins less,
because after fees a very small half profit plus a break-even rest can end slightly negative.

## Owner's goal for the trial month

80-90% of the month's closed trades in profit. The app's Trades tab shows the month's report
(`GET /report`). Every closed trade is written to `server/journal/YYYY-MM.md`, which stays on
this computer, with its entry reason and its exit reason (stop, break-even after the half
profit, trend turned, 40 days, or owner/limits).

## What was tried and rejected

- Trailing stops after the half profit
- Re-entering on pullbacks
- Faster EMAs (10/50)
- A faster BTC filter
- Mean reversion, which won 63-67% of trades but lost money
- Close full targets, which won 72% of trades but made little
- Monthly automatic strategy switching, which lost money
- Dropping AVAX (38% wins over 13 trades in 2021-2026). Tested walk-forward: in 2021-2023
  it was not weak (1 winning trade), so that data gives no reason to drop it; in 2024-2026
  dropping it raised wins from 77.6% to 79.9% but lowered the result from $62.55 to $57.70.
  Kept (2026-11-10).
- Correlation filter (skip a new trade when its coin's 30-day 4h-return correlation with any
  coin already in a trade is at or above a threshold; rules and sizing unchanged), tested
  2026-11-14 on the real engine, threshold chosen on 2021-2023 and judged on 2024-2026. In
  2021-2023 it looked strong (0.7: recovery 251 -> 67 days, drawdown 5.1% -> 2.8%). In 2024-2026
  every threshold had fewer winning trades (112 -> 99/87/75) and fewer days with 2+ trades
  (28 -> 22/17/10); 0.8 and 0.7 gave the same profit (+$39.7 / +$39.0), the same 64-day
  recovery and the same worst month (2025-06); only 0.6 steadied the curve (drawdown 4.4%,
  70% profitable months) at a cost of 41% of the profit ($39.7 -> $23.3). The worst month's
  losses came from trades opened on different days, which such a filter cannot stop.
  Rejected by the owner (2026-11-14).
- "Sonic Fast" (second strategy: buying pullbacks inside Sonic's 4h uptrend on 15m/30m/1h,
  aiming at 2+ trades per suitable day; design docs/superpowers/specs/2026-11-14-sonic-fast-design.md,
  code research/sonic_research/fast.py). Grid of 144 settings per timeframe on 2021-2023 only,
  costs 0.14%, limits fixed in advance (PF >= 1.2, drawdown <= 10%, >= 1.5 trades per suitable
  day). No setting passed on any timeframe, so nothing was frozen and the 2024-2026 hold-out was
  never run. The busiest settings lost money (15m: 1.14 trades/suitable day, -$13.7, 46% drawdown;
  1h: 0.36/day, -$4.7); the profitable ones traded rarely (15m 0.03/day; 1h 0.28/day, +$2.4 over
  three years). Fees took 21-33% of gross profit on the busy settings. Rejected (2026-11-14).

## Watching the bot: the candle simulator

The app's candle tab (`GET /simulate`) replays a period candle by candle: the bot's EMAs,
each trade's entry, half profit and exit appear when they happen, with a running count of
trades, winners and profit. Two sources: the bot's own trades now (paper or demo, with the
candle still forming, refreshed each minute) and the five-year engine replay
(`server/replay/trades.json`), by half-year.
