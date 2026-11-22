# Sonic Fast Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A paper "fast desk" inside the Sonic process that trades SOL/XRP intrabar (up to 2 per coin per day, lab day rules) beside the untouched 4h strategy, and re-chooses its lab variant weekly from the lab's results.

**Architecture:** `fastlearn.py` = pure weekly selection from the lab SQLite (read-only). `fastdesk.py` = own tables in `sonic.db`, one active `intrabar.core.Engine` variant, ×1 paper balance, aggTrade stream task that never raises into Sonic. `api.py` gets `GET /fast` and starts the desk task in the lifespan beside `engine.run_forever()`.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, websockets 17.4, pytest, ruff (server/.venv).

**Spec:** `docs/superpowers/specs/2026-11-23-sonic-fast-desk-design.md`

## Global Constraints
- Paper only: no order code, no API key read by the desk.
- Never write to Sonic's `trades`/`events`/`kv` rows used by the 4h strategy; desk tables are `fast_trades`, `fast_events`, `fast_choices`.
- Leverage ×1; balance 30.0 USDT; each trade uses the whole desk balance: `balance *= 1 + net`.
- Symbols `("SOLUSDT", "XRPUSDT")`; stream `wss://fstream.binance.com/market/stream`.
- Learning: Monday 00:00 UTC, window 14 days, min 20 trades, exclude a variant with 3 consecutive negative weekly means ending in the last 4 weeks; none qualifies → keep current; all qualifying negative → pick best, `no_profitable=True`.
- Start variant `B2-1m-q1-t0.1-all_day`.
- Desk is enabled only when env `SONIC_FAST_LAB_DB` and `SONIC_FAST_THRESHOLDS` point to existing files; otherwise it logs `fast_desk_off` once and does nothing.
- Every commit message ends with `[skip ci]`. No merge to main. No server deploy in this plan.

## Review Focus
- Lab DB missing, locked or empty at review time → keep current variant, log reason, no crash (Task 1 test).
- Variant switch due while an order/position is open → switch deferred until flat (Task 2 test).
- Stream silent/disconnected → open paper order/position dropped and logged, balance unchanged (Task 2 test).
- Any exception inside the desk → Sonic engine keeps running and `/status` still answers (Task 3 test).
- Server restart → balance, active variant and today's trades restored from `sonic.db` (Task 2 test).

---

### Task 1: Weekly selection (`fastlearn.py`)

**Files:**
- Create: `server/sonic/fastlearn.py`
- Test: `server/tests/test_fastlearn.py`

**Interfaces:**
- Produces: `@dataclass Choice(variant: str, changed: bool, no_profitable: bool, reason: str, stats: dict)`; `choose(lab_db: str, now_ms: int, current: str) -> Choice`; `is_review_due(last_review_ms: int, now_ms: int) -> bool` (true when a Monday 00:00 UTC lies in `(last_review_ms, now_ms]`).

- [ ] **Step 1: Write failing tests** (lab DB built in `tmp_path` with the lab `trades` schema from `intrabar/run.py`):
  - `test_picks_best_mean_with_enough_trades`: A 25 trades mean +0.001, B 25 trades mean +0.002 → `variant == "B"`, `changed is True`.
  - `test_too_few_trades_ignored`: B 19 trades mean +0.01, A 25 trades +0.001 → `"A"`.
  - `test_none_qualifies_keeps_current`: all < 20 trades → `variant == current`, `changed is False`, `"min_trades"` in reason.
  - `test_all_negative_picks_least_bad_and_warns`: → best negative, `no_profitable is True`.
  - `test_three_negative_weeks_excluded`: B best in window but negative weekly means for 3 consecutive weeks within last 4 → not chosen.
  - `test_missing_or_broken_db_keeps_current`: path does not exist → current, reason starts `"lab_unreadable"`.
  - `test_review_due_on_monday_boundary`: Sunday 23:59 → Monday 00:01 is due; Monday 00:01 → Monday 23:00 is not.
- [ ] **Step 2:** Run `server/.venv/Scripts/python -m pytest -q server/tests/test_fastlearn.py` → FAIL (module missing).
- [ ] **Step 3:** Implement. Open DB with `file:{path}?mode=ro` URI; any `sqlite3.Error`/`OSError` → keep current. Weekly means by `closed // (7*86400)` aligned to Monday (epoch was Thursday: offset 3 days).
- [ ] **Step 4:** Tests pass; `ruff check server/sonic/fastlearn.py` clean.
- [ ] **Step 5:** Commit `Fast desk: weekly variant selection from the lab [skip ci]`.

### Task 2: Desk state and paper accounting (`fastdesk.py`, no network)

**Files:**
- Create: `server/sonic/fastdesk.py`
- Test: `server/tests/test_fastdesk.py`

**Interfaces:**
- Consumes: `intrabar.core.Engine, Variant`; `intrabar.thresholds.load`; Task 1 `choose`, `is_review_due`.
- Produces: `class FastDesk(db: sqlite3.Connection, lab_db: str, thresholds: dict, now_ms: Callable[[], int])` with `on_trade(symbol, time_ms, price, qty, buyer_is_maker) -> list[dict]`, `drop_open(reason: str) -> None`, `review() -> Choice | None`, `status() -> dict`; `parse_variant(name: str) -> Variant`; `START_VARIANT`, `START_BALANCE = 30.0`. Imports `intrabar` by putting the repo root (`Path(__file__).resolve().parents[2]`) on `sys.path` if `import intrabar` fails.

- [ ] **Step 1: Write failing tests** (in-memory sqlite, synthetic trades, fixed clock):
  - `test_tables_are_separate`: after a closed paper trade, Sonic `Store` `trades` table has 0 rows; `fast_trades` has 1.
  - `test_balance_compounds_at_x1`: one trade with net −0.0007 → `status()["balance"] == pytest.approx(30 * (1 - 0.0007))`.
  - `test_parse_variant_roundtrip`: `parse_variant(v.name) == v` for all 72 variants of `intrabar.run.all_variants()`.
  - `test_switch_deferred_until_flat`: review returns new variant while a position is open → active stays old; after the close, active becomes new and a `fast_choices` row exists.
  - `test_drop_open_on_gap`: with a pending order, `drop_open("disconnected")` → no pending/position, balance unchanged, `fast_events` row `dropped_on_gap`.
  - `test_restart_restores_state`: new `FastDesk` on the same db → same balance, active variant, and today's per-coin counts (day rules continue).
  - `test_status_warns_when_no_profitable`: last choice `no_profitable` → `status()["warning"] == "no_profitable_variant"`.
  - `test_leverage_shown_only_as_info`: `status()["x24_info_pct"] == 24 * status()["total_net_pct"]`, balance unaffected.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Rebuild today's day state on start by replaying today's `fast_trades` rows into the `Account.days` structure (per-coin counts, wins/losses, stopped flag).
- [ ] **Step 4:** Tests pass; ruff clean.
- [ ] **Step 5:** Commit `Fast desk: paper accounting, separate tables, deferred switching [skip ci]`.

### Task 3: Stream, schedule, isolation and `/fast`

**Files:**
- Modify: `server/sonic/fastdesk.py` (add `async def run_forever(self) -> None` and `from_env(db: sqlite3.Connection, now_ms) -> FastDesk | None`)
- Modify: `server/sonic/api.py` (lifespan starts the desk task when `background`; add `GET /fast`, secured)
- Modify: `server/sonic/engine.py` (only: `self.fast = fastdesk.from_env(store.db, self.now_ms)` in `__init__`)
- Test: `server/tests/test_fastdesk_api.py`

**Interfaces:**
- Consumes: Task 2 `FastDesk`.
- Produces: `GET /fast` → `{"enabled": bool, ...status()}`; `{"enabled": false}` when off.

- [ ] **Step 1: Write failing tests:**
  - `test_fast_route_off_without_env`: no env → `/fast` 200 `{"enabled": False}`; `/status` unchanged.
  - `test_fast_route_needs_token`: no token → 401.
  - `test_fast_route_with_desk`: env pointing to tmp lab DB + thresholds → `enabled True`, `variant == "B2-1m-q1-t0.1-all_day"`, `balance == 30.0`.
  - `test_desk_exception_does_not_stop_sonic`: monkeypatch desk `on_trade` to raise; feed one message through the loop's handler → exception logged to `fast_events` `desk_error`, Sonic `engine.halted == ""`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `run_forever`: same loop as `intrabar/run.py` `stream()` (30 s silence watchdog, backoff 1→60 s, `drop_open` on gap), plus `review()` check once a minute, every handler error caught and logged as `desk_error` (no re-raise except `CancelledError`).
- [ ] **Step 4:** Full server suite `server/.venv/Scripts/python -m pytest -q server/tests intrabar/tests` passes; `ruff check server intrabar` clean.
- [ ] **Step 5:** Commit `Fast desk: live stream, weekly review, /fast route, isolated from Sonic [skip ci]`.

### Task 4: Local live check and handoff

- [ ] **Step 1:** Local run: copy the lab DB from the server (`scp` read-only copy) and `D:\Sonic-Intrabar-Research\paper\thresholds.json`; start Sonic with the two env vars on a scratch `.env`/db copy for 5 minutes; `GET /fast` shows `enabled`, events show `connected`.
- [ ] **Step 2:** Push branch `feature/sonic-fast-desk`. Report to the owner in Arabic and ask for separate deploy approval (one Sonic restart, env vars added to `/opt/sonic/app/server/.env`).
