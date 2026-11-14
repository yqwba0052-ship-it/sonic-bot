# Sonic Fast (research) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A research module that backtests «Sonic السريع» (buying pullbacks inside Sonic's 4h uptrend) on 15m, 30m and 1h bars, selects settings on 2021–2023 only, freezes them, judges them once on 2024–2026, and reports the owner's metrics alone and together with Sonic.

**Architecture:** Numpy indicators per coin and timeframe; a 4h regime aligned to fast bars from the last *closed* 4h bar; candidate entries at the next bar's open; independent exit scan per candidate; one chronological portfolio pass that enforces the max-open, coin-conflict, daily and combined planned-risk limits; metrics and a frozen-config runner. Nothing in `server/` changes.

**Tech Stack:** Python 3.13, numpy, pytest, ruff (research venv as in CI: `pip install numpy pytest ruff fastapi httpx pydantic`), Binance public data via `sonic_research.data.load_bars`.

**Spec:** `docs/superpowers/specs/2026-11-14-sonic-fast-design.md`

## Global Constraints

- Do not modify anything under `server/` or Sonic's strategy, settings or replay file; no real trading.
- Long only. Leverage 3. Risk per trade 0.5% of Sonic Fast's own equity, never raised after a loss.
- At most 2 open Sonic Fast trades; one trade per coin; skip any coin that has an open Sonic trade.
- Daily limit: realized losses today (with fees) + planned risk of open trades + new trade's planned risk (with fees) ≤ 2% of Sonic Fast equity; else no entry until next UTC day.
- Combined limit: planned risk of open Sonic trades + open Sonic Fast trades + new trade ≤ 4% of combined equity (Sonic $30 + Sonic Fast $30). It is a limit on *planned* risk at the stop, not a guarantee of maximum loss.
- Base round-trip cost 0.14% (fee 0.05% ×2 + slippage 0.02% ×2); sensitivity at 0.20% and 0.30% with no setting changes. Fee-coverage filter fixed at target distance ≥ 0.42% of price.
- Regime (from last closed 4h bar): BTC close > EMA300(4h); coin close > EMA200(4h); coin EMA20 > EMA100 (4h).
- Reversal guards: fast close ≥ fast EMA100; fast EMA50 slope over 10 bars ≥ 0; coin last 4h close ≥ 4h EMA20; BTC crash guard = fixed (drop > 2% over last 4h) **or** volatility (drop > 1.5 × BTC ATR14% on 4h), chosen on 2021–2023 only.
- Entry: RSI14 dipped below L within last k bars, last closed bar closes above the previous bar's high and RSI ≥ L; fill at next bar open + slippage. Stop = entry − ks×ATR14, target = entry + kt×ATR14, time exit after H bars (96/48/24 for 15m/30m/1h); stop first when both are touched in one bar.
- Grid: L ∈ {30,35,40}, k ∈ {3,6}, ks ∈ {1.0,1.5,2.0}, kt ∈ {1.0,1.5,2.0,3.0}, guard ∈ {fixed, vol}. Selection on 2021-01..2023-12: max net profit after 0.14% cost subject to PF ≥ 1.2, drawdown ≤ 10%, ≥ 1.5 trades per suitable day.
- Frozen settings are written with a sha256 before any 2024–2026 run; the hold-out is run once; no change after seeing it.
- Acceptance on 2024-01..2026-10 (unchanged, never lowered): net > 0, PF ≥ 1.2, drawdown ≤ 10%, ≥ 1.5 trades per suitable day, combined worst month ≤ Sonic-alone worst month + 2 percentage points. If every timeframe fails: reject.
- Coins: SOLUSDT XRPUSDT DOGEUSDT ADAUSDT SUIUSDT TRXUSDT BNBUSDT AVAXUSDT; BTCUSDT for the regime. Data 2020-06..2026-10.

## Review Focus

- A coin listed mid-period (SUI from 2023): no bars before listing → no signals, no crash; its days still count only when its regime is true.
- Gaps in Binance files (missing bars): fast-bar to 4h alignment must still use only 4h bars closed at or before the fast bar's close.
- ATR of zero or NaN at the start of a series: no candidate, no division error.
- A trade still open at the end of a period: closed at the last close and marked, never dropped.
- Cost 0.30%: the same frozen candidates, only PnL changes (the fee-coverage filter stays at 0.42%).

---

### Task 1: Regime aligned to fast bars without look-ahead

**Files:**
- Create: `research/sonic_research/fast.py`
- Test: `research/tests/test_fast.py`

**Interfaces:**
- Produces: `ema(x: np.ndarray, n: int) -> np.ndarray`, `rsi(c, n=14) -> np.ndarray`, `atr(h, low, c, n=14) -> np.ndarray`, `closed_4h_index(fast_t: np.ndarray, fast_ms: int, t4: np.ndarray) -> np.ndarray` (index of the last 4h bar whose close time ≤ the fast bar's close time, −1 if none), `regime(fast_t, fast_ms, coin4: Bars, btc4: Bars) -> dict[str, np.ndarray]` with boolean `ok` and float arrays `btc_drop_4h`, `btc_atr_pct_4h`, `coin_above_ema20_4h`.

- [ ] **Step 1: Write the failing tests** — `test_regime_uses_only_closed_4h_bars` (a 4h bar opening at 08:00 is not visible to a 15m bar closing at 11:45, visible to one closing at 12:00); `test_regime_needs_btc_coin_and_trend` (each of the three conditions false turns `ok` false); `test_missing_bars_do_not_shift_alignment` (drop two 4h bars; indices still point to closed bars only); `test_coin_listed_late_is_never_ok_before_listing`.
- [ ] **Step 2: Run** `pytest research/tests/test_fast.py -q` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the functions above (searchsorted on 4h close times).
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Commit** `git add research/sonic_research/fast.py research/tests/test_fast.py && git commit -m "Sonic Fast research: 4h regime aligned to fast bars without look-ahead"`

### Task 2: Candidate entries with reversal guards and fee coverage

**Files:** Modify `research/sonic_research/fast.py`; Test `research/tests/test_fast.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `@dataclass(frozen=True) class Params: L: int; k: int; ks: float; kt: float; guard: str  # "fixed"|"vol"`; `@dataclass(frozen=True) class Candidate: symbol: str; i: int; t_ms: int; entry: float; stop: float; target: float; atr: float`; `candidates(symbol: str, fast: Bars, fast_ms: int, reg: dict, p: Params, slip: float = 0.0002) -> list[Candidate]`.

- [ ] **Step 1: Write the failing tests** — `test_entry_is_next_open_plus_slippage`; `test_future_bars_do_not_change_past_candidates` (mutate every bar after a candidate's signal bar; the candidate list up to it is identical); `test_guards_block_entries` (below fast EMA100, falling EMA50, coin under 4h EMA20, BTC crash fixed 2% / vol 1.5×ATR each block); `test_fee_coverage_blocks_small_targets` (kt×ATR/price < 0.42% → none); `test_zero_atr_gives_no_candidate`.
- [ ] **Step 2: Run** `pytest research/tests/test_fast.py -q` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** `candidates` (signal at closed bar i; fill at open[i+1]×(1+slip); stop/target from atr[i]).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `"Sonic Fast research: pullback entries, reversal guards, fee coverage"`

### Task 3: Exits, costs and sizing

**Files:** Modify `research/sonic_research/fast.py`; Test `research/tests/test_fast.py`

**Interfaces:**
- Consumes: Task 2.
- Produces: `exit_of(c: Candidate, fast: Bars, hold: int) -> tuple[int, float, str]` (exit bar index, exit price, "stop"|"target"|"time"|"end"); `sized(c: Candidate, equity: float, cost: float, leverage: int = 3, risk: float = 0.005) -> tuple[float, float]` (qty, planned_risk_usd including round-trip cost at the stop); `pnl(c, qty, exit_price, cost) -> float`.

- [ ] **Step 1: Write the failing tests** — `test_stop_first_when_both_touched`; `test_time_exit_after_hold_bars`; `test_open_at_end_is_closed_at_last_close_and_marked_end`; `test_planned_risk_is_half_percent_with_costs` (equity 30, stop 1% → planned risk 0.15 incl. cost); `test_position_size_differs_from_risk` (qty×entry = notional ≈ 15 for that case, margin = notional/3); `test_cost_only_changes_pnl` (same qty for 0.14/0.20/0.30, pnl lower as cost rises).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** the three functions (cost split half at entry, half at exit).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `"Sonic Fast research: exits, costs and sizing by planned risk"`

### Task 4: Portfolio pass with Sonic overlap and planned-risk limits

**Files:** Modify `research/sonic_research/fast.py`; Test `research/tests/test_fast.py`

**Interfaces:**
- Consumes: Tasks 2–3; Sonic trades from `server/replay/trades.json` (read-only: `symbol, opened_ms, closed_ms, risk_usdt`).
- Produces: `@dataclass class Trade: symbol, opened_ms, closed_ms, entry, exit, qty, pnl, planned_risk, why`; `run(cands: list[Candidate], bars: dict[str, Bars], hold: int, cost: float, sonic: list[dict], start_ms: int, end_ms: int) -> list[Trade]`.

- [ ] **Step 1: Write the failing tests** — `test_max_two_open`; `test_skips_coin_with_open_sonic_trade`; `test_daily_limit_counts_realized_open_and_new_risk_with_fees` (after realized −1.5% and one open 0.5% trade, a new 0.5% entry is refused; next UTC day allowed); `test_combined_limit_with_sonic_open_risk` (Sonic open planned risk 2.1 of 60 + two fast trades would pass 4% → refused); `test_no_size_increase_after_loss` (qty/equity ratio unchanged after a losing trade).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** `run` (candidates sorted by fill time; equity updated at exits in time order).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `"Sonic Fast research: portfolio limits and Sonic overlap"`

### Task 5: Metrics and the owner's separate report

**Files:** Modify `research/sonic_research/fast.py`; Test `research/tests/test_fast.py`

**Interfaces:**
- Produces: `metrics(trades: list[Trade], suitable_days: set[date], first: date, last: date, capital: float) -> dict` with keys `trades, per_suitable_day, suitable_days, days_without_trade, days_2plus, win_rate, profit_factor, net_pnl, fee_share, max_drawdown, worst_month, months_positive, longest_pause_days, recovery_days_median, recovery_days_max, worst_loss_vs_planned`; `suitable_days(regimes: dict[str, dict], fast_t: dict, ms: int) -> set[date]`.

- [ ] **Step 1: Write the failing tests** — `test_metrics_on_known_trades` (hand-built 4 trades: PF, net, drawdown, recovery, days_2plus, per_suitable_day exact); `test_worst_loss_vs_planned_reports_gap_beyond_stop`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** `metrics`, `suitable_days`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `"Sonic Fast research: metrics and separate report"`

### Task 6: Train, freeze, hold-out run and verdict

**Files:**
- Create: `research/sonic_research/fast_run.py`
- Create (output): `research/reports/sonic_fast_frozen.json`, `research/reports/sonic_fast.json`
- Test: `research/tests/test_fast.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `select(results: list[dict]) -> dict | None` (selection rule from Global Constraints); `freeze(choice: dict, path: Path) -> str` (writes JSON + sha256); `verdict(test: dict, sonic_worst_month_pct: float) -> tuple[bool, list[str]]`; CLI `python -m sonic_research.fast_run train` then `python -m sonic_research.fast_run holdout` (refuses to run hold-out without a frozen file, refuses a second hold-out run unless `--force`).

- [ ] **Step 1: Write the failing tests** — `test_select_respects_constraints_and_picks_max_net`; `test_holdout_refuses_without_frozen_file`; `test_verdict_rejects_each_failed_criterion` (net ≤ 0, PF < 1.2, DD > 10%, < 1.5/suitable day, worst month +> 2 pp).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** `fast_run.py` (grid over Params for each of 15m/30m/1h on 2021-01..2023-12 at cost 0.14%; freeze; hold-out at 0.14/0.20/0.30 with frozen settings; combined with Sonic; write `sonic_fast.json`).
- [ ] **Step 4: Run** `pytest research/tests -q` and `ruff check research` — Expected: all pass; then `python -m sonic_research.fast_run train` and `python -m sonic_research.fast_run holdout` (downloads ~100 MB of public data the first time).
- [ ] **Step 5: Commit** code, tests and both report files: `"Sonic Fast research: frozen train selection and one hold-out run"`; record the outcome (accepted or rejected, with numbers) in `STRATEGY.md`.
