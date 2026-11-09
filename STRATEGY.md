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
3. Longs only. Shorts lost money in 2024-2026 while longs won every year. The owner can allow
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
- **Drift alarm:** the last 30 trades averaging below -0.30R stops new entries. In 6 years of
  history the worst 30-trade average was -0.23R.
- **Network outages:** the bot rides them out; it halts after 30 minutes without a break.

## Exit

1. **Stop:** 2.5 x ATR(14) below the entry, placed on the exchange.
2. **Half profit:** half the position closes at +0.25 x the stop distance. The stop on the rest
   then moves to the entry price, so most trades end in profit.
3. **The rest** exits when the EMAs cross back, or after 40 days.

## What history says

On the real engine, 2021-06..2026-10, a $30 account:

| | |
|---|---|
| Trades | 266 |
| Winning trades | 80.8% |
| End amount | $86.72 |
| Deepest drop | 15% |

By period: 2022 (bear market), 95.5% winning and $30 became $31.72. 2025-07..2026-10, 74.6%
winning and $30 became $33.98. Past results are no promise.

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
