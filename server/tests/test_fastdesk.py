"""Fast desk: paper accounting, separate tables, experience memory, River shadow mode."""

from __future__ import annotations

import json

import pytest

from sonic import fastmind
from sonic.fastdesk import START_BALANCE, START_VARIANT, FastDesk, all_variants, parse_variant
from sonic.fastlearn import Choice
from sonic.store import Store

T0 = 1_795_392_000  # Monday 2026-11-23 00:00:00 UTC (seconds), inside a session window
# Only B2 on 1-minute candles with q=1% can fire (B1 needs an impossible rise).
THR = {"SOLUSDT": {60: {0.01: {"rise": 9.0, "buyshare30": 0.0}}}}
B2_1M_Q1 = {v.name for v in all_variants() if v.signal == "B2" and v.tf == 60 and v.q == 0.01}


def make(tmp_path, now_s: int = T0 + 50, model: bool = True) -> tuple[FastDesk, Store]:
    store = Store(str(tmp_path / "sonic.db"))
    clock = {"ms": now_s * 1000}
    desk = FastDesk(store.db, THR, lambda: clock["ms"],
                    str(tmp_path / "model.pkl") if model else None)
    desk.clock = clock  # tests move time through this
    return desk, store


def one_trade(desk: FastDesk, win: bool, start: int = T0) -> list[dict]:
    """Rising seconds -> B2 limit at 100.01 -> filled at 100.005 -> target or stop."""
    out = []
    for i in range(12):
        out += desk.on_trade("SOLUSDT", (start + i) * 1000, 100 + i * 0.01, 1, False)
    out += desk.on_trade("SOLUSDT", (start + 12) * 1000, 100.005, 1, True)
    exit_price = 100.01 * (1.0035 if win else 0.996)
    out += desk.on_trade("SOLUSDT", (start + 13) * 1000, exit_price, 1, not win)
    return out


def test_tables_are_separate_from_sonic(tmp_path) -> None:
    desk, store = make(tmp_path)
    one_trade(desk, True)
    assert store.trades(10) == [] and store.events(10) == []
    assert desk.db.execute("SELECT COUNT(*) FROM fast_trades").fetchone()[0] == 1


def test_every_shadow_candidate_is_recorded(tmp_path) -> None:
    desk, _ = make(tmp_path)
    one_trade(desk, True)
    rows = desk.db.execute("SELECT variant, features, net, kind FROM fast_experience").fetchall()
    assert {r[0] for r in rows} == B2_1M_Q1 and len(rows) == 6
    feats = json.loads(rows[0][1])
    assert set(feats) == {"rise", "up10", "breakout", "buyshare30", "in_session", "tf", "hour",
                          "vol60"}
    assert all(r[2] is not None and r[3] == "target" for r in rows)


def test_balance_compounds_at_x1(tmp_path) -> None:
    desk, _ = make(tmp_path)
    rec = one_trade(desk, False)[0]
    assert rec["variant"] == START_VARIANT and rec["kind"] == "stop"
    assert desk.status()["balance"] == pytest.approx(START_BALANCE * (1 + rec["net"]), abs=1e-6)
    assert desk.status()["leverage"] == 1


def test_leverage_shown_only_as_info(tmp_path) -> None:
    desk, _ = make(tmp_path)
    one_trade(desk, True)
    st = desk.status()
    assert st["x24_info_pct"] == pytest.approx(24 * st["total_net_pct"], abs=1e-3)
    assert st["balance"] == pytest.approx(START_BALANCE * (1 + st["last_trades"][0]["net"]))


def test_parse_variant_roundtrip() -> None:
    assert len(all_variants()) == 72
    for v in all_variants():
        assert parse_variant(v.name) == v


def test_switch_deferred_until_flat(tmp_path) -> None:
    desk, _ = make(tmp_path)
    for i in range(12):  # active variant now has a pending order
        desk.on_trade("SOLUSDT", (T0 + i) * 1000, 100 + i * 0.01, 1, False)
    assert desk.engine.accounts[START_VARIANT].pending
    desk.wanted = "B2-1m-q1-t0.3-all_day"
    desk._maybe_switch()
    assert desk.active == START_VARIANT
    desk.on_trade("SOLUSDT", (T0 + 12) * 1000, 100.005, 1, True)
    desk.on_trade("SOLUSDT", (T0 + 13) * 1000, 100.01 * 1.0035, 1, False)
    assert desk.active == "B2-1m-q1-t0.3-all_day"
    assert desk.db.execute("SELECT COUNT(*) FROM fast_events WHERE code='variant_switched'"
                           ).fetchone()[0] == 1


def test_weekly_review_records_choice(tmp_path) -> None:
    desk, _ = make(tmp_path)
    desk.clock["ms"] += 8 * 86_400_000  # past next Monday
    choice = desk.review()
    assert isinstance(choice, Choice) and not choice.changed  # too few trades: keep
    assert desk.db.execute("SELECT COUNT(*) FROM fast_choices").fetchone()[0] == 1
    assert desk.review() is None  # not due again this week


def test_drop_open_on_gap(tmp_path) -> None:
    desk, _ = make(tmp_path)
    for i in range(12):
        desk.on_trade("SOLUSDT", (T0 + i) * 1000, 100 + i * 0.01, 1, False)
    desk.drop_open("disconnected")
    assert all(not a.pending and not a.position for a in desk.engine.accounts.values())
    assert desk.balance == START_BALANCE
    assert desk.db.execute("SELECT COUNT(*) FROM fast_events WHERE code='dropped_on_gap'"
                           ).fetchone()[0] == 1


def test_restart_restores_state(tmp_path) -> None:
    desk, _ = make(tmp_path)
    one_trade(desk, True)
    one_trade(desk, False, start=T0 + 120)
    balance = desk.balance
    again, _ = make(tmp_path, now_s=T0 + 300)
    assert again.balance == pytest.approx(balance) and again.active == START_VARIANT
    acc = again.engine.accounts[START_VARIANT]
    assert acc.day(T0)["per_coin"]["SOLUSDT"] == 2
    assert not acc.can_open("SOLUSDT", T0 + 400)


def test_status_warns_when_no_profitable(tmp_path) -> None:
    desk, _ = make(tmp_path)
    desk._put("no_profitable", True)
    assert desk.status()["warning"] == "no_profitable_variant"


def test_mind_absent_is_harmless(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fastmind, "compose", None)
    desk, _ = make(tmp_path)
    one_trade(desk, True)
    assert not desk.mind.available
    assert desk.db.execute("SELECT COUNT(*) FROM fast_experience WHERE predicted IS NULL"
                           ).fetchone()[0] == 6


def test_prediction_stored_before_outcome(tmp_path) -> None:
    pytest.importorskip("river")
    desk, _ = make(tmp_path)
    one_trade(desk, True)
    preds = [r[0] for r in desk.db.execute("SELECT predicted FROM fast_experience")]
    assert all(p == pytest.approx(0.5) for p in preds)  # untrained model: no knowledge yet


def test_rebuild_reproduces_model(tmp_path) -> None:
    pytest.importorskip("river")
    desk, _ = make(tmp_path)
    for k in range(6):
        one_trade(desk, k % 3 != 0, start=T0 + 120 * k)
    probes = [{"rise": 0.001 * i, "up10": i % 2 == 0, "breakout": True, "buyshare30": 0.6,
               "in_session": True, "tf": 60, "hour": i, "vol60": 0.002} for i in range(5)]
    before = [desk.mind.predict(p) for p in probes]
    (tmp_path / "model.pkl").unlink(missing_ok=True)
    fresh = fastmind.Mind(desk.db, str(tmp_path / "model.pkl"))
    assert fresh.learned == desk.mind.learned > 0
    assert [fresh.predict(p) for p in probes] == pytest.approx(before, abs=1e-9)


def test_shadow_never_filters(tmp_path, monkeypatch) -> None:
    desk, _ = make(tmp_path)
    monkeypatch.setattr(desk.mind, "predict", lambda features: 0.0)
    assert one_trade(desk, True)  # the active variant still trades


def test_eligibility_thresholds(tmp_path) -> None:
    desk, _ = make(tmp_path)
    mind = desk.mind

    def fill(n: int) -> None:
        desk.db.execute("DELETE FROM fast_experience")
        for i in range(n):
            good = i % 2 == 0
            desk.db.execute(
                "INSERT INTO fast_experience (variant, symbol, decided, closed, features, "
                "predicted, net, kind) VALUES ('v','SOLUSDT',?,?,'{}',?,?,'x')",
                (T0, T0, 0.9 if good else 0.1, 0.001 if good else -0.001))

    fill(299)
    assert not mind.shadow_report(T0 * 1000)["eligible_for_owner_review"]
    fill(300)
    report = mind.shadow_report(T0 * 1000)
    assert report["eligible_for_owner_review"] and report["mode"] == "shadow"
