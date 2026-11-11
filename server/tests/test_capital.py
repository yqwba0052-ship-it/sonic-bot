"""Money for approved strategies (capital.py): approve, allocate and activate are separate, and
nothing trades outside its amount, its coins or its limits. A fake exchange records every call."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from sonic import account, capital, leader
from sonic.api import create_app
from sonic.exchange import Closed, Fill, Position
from sonic.risk import Filters
from sonic.strategy import ema
from tests.test_lab import END, MARKET
from tests.test_system import build

MODEL = {"sides": ["BUY"], "trend": "with", "btc": {}, "rsi": [0, 100], "dist": [-99, 99],
         "age": [0, 500], "stop_atr": 2.0, "target_atr": 2.0, "max_hold_bars": 10,
         "symbols": ["SOLUSDT", "XRPUSDT"], "leverage": 3}
SOL = MARKET["SOLUSDT"]
_closes = [b.c for b in SOL]
_fast, _slow = ema(_closes, 20), ema(_closes, 100)
CROSS = next(i for i in range(1500, len(SOL))
             if _fast[i] > _slow[i] and _fast[i - 1] <= _slow[i - 1])


class FakeExchange:
    def __init__(self) -> None:
        self.positions_: dict[str, Position] = {}
        self.calls: list[tuple] = []
        self.fail_protect = False

    async def sync_time(self) -> None: ...

    async def positions(self) -> dict[str, Position]:
        return dict(self.positions_)

    async def filters(self, symbol: str) -> Filters:
        return Filters("0.001", "0.01", 0.001, 5.0)

    async def prepare(self, symbol: str, leverage: int) -> None:
        self.calls.append(("prepare", symbol, leverage))

    async def open_market(self, symbol: str, side: str, qty: float, client_id: str) -> Fill:
        price = SOL[CROSS].c if symbol == "SOLUSDT" else 1.0
        self.positions_[symbol] = Position(symbol, qty if side == "BUY" else -qty, price, price, 0)
        self.calls.append(("open", symbol, side, qty))
        return Fill(price, qty)

    async def protect(self, symbol, side, stop, target, target_qty=0.0) -> None:
        self.calls.append(("protect", symbol, stop, target))
        if self.fail_protect:
            raise RuntimeError("rejected")

    async def close_market(self, symbol: str, qty: float, client_id: str) -> None:
        self.calls.append(("close", symbol, qty))
        self.positions_.pop(symbol, None)

    async def closed_result(self, symbol: str, since_ms: int) -> Closed:
        return Closed(90.0, -2.5, 0.05)


def setup(allowed: bool = False) -> tuple[capital.Capital, leader.Lab, int]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    lab = leader.Lab(db, lambda: END)
    lab.add("aaa", "A")
    cur = db.execute("INSERT INTO trader_models (trader_id, created_ms, model, report) "
                     "VALUES ('aaa', ?, ?, '{}')", (END, json.dumps(MODEL)))
    db.commit()
    return capital.Capital(db, lab, lambda: END, allowed=lambda: allowed), lab, cur.lastrowid


def passed_out_of_sample(lab: leader.Lab, model_id: int) -> None:
    lab.put_state("coins", {"made_ms": END, "coins": {"SOLUSDT": {
        "ranked": [{"model_id": model_id}], "excluded": [], "best": None}}})


def paper_wins(cap: capital.Capital, model_id: int, n: int = 20) -> None:
    for k in range(n):
        ret = 0.02 if k % 5 else -0.01
        cap.db.execute(
            "INSERT INTO trader_paper (trader_id, model_id, symbol, side, entry_price, stop_price,"
            " target_price, opened_ms, until_ms, closed_ms, exit_price, ret, reason) VALUES "
            "('aaa', ?, 'SOLUSDT', 'BUY', 100, 98, 104, ?, ?, ?, 102, ?, 'target')",
            (model_id, k, k + 1, k + 1, ret))
    cap.db.commit()


BOUNDS = {"min": 10.0, "max": 50.0, "available": 80.0}


def ready(allowed: bool = False) -> tuple[capital.Capital, int]:
    cap, lab, mid = setup(allowed)
    passed_out_of_sample(lab, mid)
    cap.approve(mid)
    cap.allocate(mid, 40, BOUNDS)
    cap.set_limits(mid, 10, 4)
    paper_wins(cap, mid)
    return cap, mid


TRADE_KEY = {"key": "k" * 20, "secret": "s" * 20, "problems": []}


# ---------- step 1 and 2

def test_only_a_strategy_that_passed_out_of_sample_can_be_approved() -> None:
    cap, lab, mid = setup()
    with pytest.raises(ValueError, match="not_eligible"):
        cap.approve(mid)
    passed_out_of_sample(lab, mid)
    view = cap.approve(mid)
    assert view["state"] == "approved" and view["amount"] == 0
    assert view["symbols"] == ["SOLUSDT", "XRPUSDT"]  # the trader's own coins


def test_bounds_and_balance_messages_and_no_double_reservation() -> None:
    cap, lab, mid = setup()
    passed_out_of_sample(lab, mid)
    cap.approve(mid)
    bounds = cap.bounds(mid, wallet=100, available=80, coin_minimums={"SOLUSDT": 12, "XRPUSDT": 7})
    assert bounds["min"] == 12 and bounds["max"] == 50  # half the wallet for one strategy
    cap.allocate(mid, 45, bounds)
    other = lab.db.execute("INSERT INTO trader_models (trader_id, created_ms, model, report) "
                           "VALUES ('aaa', ?, ?, '{}')", (END, json.dumps(MODEL))).lastrowid
    lab.put_state("coins", {"made_ms": END, "coins": {"SOLUSDT": {
        "ranked": [{"model_id": mid}, {"model_id": other}], "excluded": [], "best": None}}})
    cap.approve(other)
    second = cap.bounds(other, wallet=100, available=80, coin_minimums={"SOLUSDT": 12})
    assert second["reserved_by_others"] == 45 and second["max"] == 35  # 80 - 45: no overlap
    with pytest.raises(ValueError, match="insufficient_balance"):
        cap.allocate(other, 40, second)
    with pytest.raises(ValueError, match="below_minimum"):
        cap.allocate(other, 5, second)
    empty = cap.bounds(other, wallet=0, available=0, coin_minimums={})
    with pytest.raises(ValueError, match="no_balance"):
        cap.allocate(other, 5, empty)


def test_limits_must_fit_inside_the_amount() -> None:
    cap, lab, mid = setup()
    passed_out_of_sample(lab, mid)
    cap.approve(mid)
    cap.allocate(mid, 40, BOUNDS)
    with pytest.raises(ValueError, match="invalid_limits"):
        cap.set_limits(mid, 50, 5)  # above the amount
    with pytest.raises(ValueError, match="invalid_limits"):
        cap.set_limits(mid, 5, 8)  # daily above total
    assert cap.set_limits(mid, 10, 4)["max_loss"] == 10


# ---------- step 3

def test_activation_needs_every_condition_and_the_typed_approval() -> None:
    cap, lab, mid = setup()
    passed_out_of_sample(lab, mid)
    cap.approve(mid)
    with pytest.raises(ValueError, match="phrase_mismatch"):
        cap.activate(mid, "demo", "yes", TRADE_KEY)
    with pytest.raises(ValueError, match="gate_not_met:allocated,limits,paper"):
        cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    cap.allocate(mid, 40, BOUNDS)
    cap.set_limits(mid, 10, 4)
    paper_wins(cap, mid)
    with pytest.raises(ValueError, match="trade_key"):
        cap.activate(mid, "demo", capital.LIVE_PHRASE, None)
    with pytest.raises(ValueError, match="server_switch"):
        cap.activate(mid, "real", capital.LIVE_PHRASE, TRADE_KEY)  # switch off by default
    view = cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    assert view["state"] == "live" and view["mode"] == "demo"


def test_real_needs_the_server_switch() -> None:
    cap, mid = ready(allowed=True)
    assert cap.activate(mid, "real", capital.LIVE_PHRASE, TRADE_KEY)["mode"] == "real"
    with pytest.raises(PermissionError):
        capital.LiveClient(httpx.AsyncClient(base_url=capital.REAL_URL), "k", "s", allowed=False)
    with pytest.raises(ValueError):
        capital.LiveClient(httpx.AsyncClient(base_url="https://evil.example"), "k", "s", True)


def test_trading_key_must_not_withdraw_or_transfer() -> None:
    ok = {"enableReading": True, "enableFutures": True, "ipRestrict": True}
    assert capital.trade_key_problems(ok) == []
    assert capital.trade_key_problems({**ok, "enableWithdrawals": True}) == ["enableWithdrawals"]
    assert capital.trade_key_problems({**ok, "permitsUniversalTransfer": True,
                                       "enableInternalTransfer": True}) == [
        "enableInternalTransfer", "permitsUniversalTransfer"]
    assert capital.trade_key_problems({"enableReading": True}) == ["enableFutures_off"]


# ---------- running

def bars_until_cross(symbol: str):
    async def bars(name: str):
        return MARKET[name][: CROSS + 1]
    return bars


async def test_live_trade_stays_inside_its_amount_and_coins() -> None:
    cap, mid = ready()
    cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    fake = FakeExchange()
    fake.positions_["XRPUSDT"] = Position("XRPUSDT", 50, 1, 1, 0)  # owner's own manual trade
    logs: list = []

    async def client_for(mode: str):
        return fake

    await cap.tick(client_for, bars_until_cross("SOLUSDT"), lambda *a: logs.append(a))
    opened = [c for c in fake.calls if c[0] == "open"]
    assert [c[1] for c in opened] == ["SOLUSDT"]  # XRP skipped: a position Sonic did not open
    view = cap.view(mid)
    assert 0 < view["used"] <= view["amount"] and view["possible_loss"] > 0
    assert view["possible_loss"] <= view["amount"] * 0.011 + 0.1  # about 1% risk plus fees
    assert any(c[0] == "protect" for c in fake.calls)
    # the exchange closed it at the stop: the loss is booked against this strategy only
    fake.positions_.pop("SOLUSDT")
    await cap.tick(client_for, bars_until_cross("SOLUSDT"), lambda *a: logs.append(a))
    assert cap.view(mid)["realized"] == pytest.approx(-2.5)


async def test_failed_protection_closes_and_stops() -> None:
    cap, mid = ready()
    cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    fake = FakeExchange()
    fake.fail_protect = True

    async def client_for(mode: str):
        return fake

    logs: list = []
    await cap.tick(client_for, bars_until_cross("SOLUSDT"), lambda *a: logs.append(a))
    assert any(c[0] == "close" for c in fake.calls)
    assert cap.row(mid)["state"] == "stopped"
    assert any(a[1] == "strategy_protection_failed" for a in logs)


async def test_stop_fates_of_open_trades_and_loss_limit() -> None:
    cap, mid = ready()
    cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    fake = FakeExchange()

    async def client_for(mode: str):
        return fake

    log = lambda *a: None  # noqa: E731
    await cap.tick(client_for, bars_until_cross("SOLUSDT"), log)
    cap.stop(mid, close_open=False)  # open trades keep their stop and target
    await cap.tick(client_for, bars_until_cross("SOLUSDT"), log)
    assert not any(c[0] == "close" for c in fake.calls)
    cap.stop(mid, close_open=True)  # now close them
    await cap.tick(client_for, bars_until_cross("SOLUSDT"), log)
    assert any(c[0] == "close" for c in fake.calls)
    # total loss limit: realized beyond max_loss stops the strategy and closes everything
    cap2, mid2 = ready()
    cap2.activate(mid2, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    cap2.db.execute("UPDATE allocations SET realized = -11 WHERE model_id = ?", (mid2,))
    logs: list = []
    await cap2.tick(client_for, bars_until_cross("SOLUSDT"), lambda *a: logs.append(a))
    assert cap2.row(mid2)["state"] == "stopped"
    assert any(a[1] == "strategy_loss_limit" for a in logs)


async def test_emergency_stop_closes_only_strategy_positions() -> None:
    cap, mid = ready()
    cap.activate(mid, "demo", capital.LIVE_PHRASE, TRADE_KEY)
    fake = FakeExchange()
    fake.positions_["BTCUSDT"] = Position("BTCUSDT", 0.01, 1, 1, 0)  # not the strategy's

    async def client_for(mode: str):
        return fake

    await cap.tick(client_for, bars_until_cross("SOLUSDT"), lambda *a: None)
    closed = await cap.emergency(client_for, lambda *a: None)
    assert closed == 1 and "BTCUSDT" in fake.positions_
    assert cap.row(mid)["state"] == "stopped"


# ---------- API

def test_api_steps_are_separate(monkeypatch) -> None:
    engine, _, _, clock = build()
    clock.now = END / 1000
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        refused_approval = client.post("/allocations/1/approve", headers=headers)
        assert refused_approval.status_code == 422
        assert refused_approval.json()["detail"] == "not_eligible"
        body = client.get("/allocations", headers=headers).json()
        assert body["live_allowed"] is False and body["items"] == []
        assert client.get("/allocations/1/bounds", headers=headers).status_code == 422
        unsafe = {"enableReading": True, "enableFutures": True, "enableWithdrawals": True}
        monkeypatch.setattr(account, "transport", lambda: httpx.MockTransport(
            lambda request: httpx.Response(200, json=unsafe)))
        refused = client.put("/trade-key", json={"key": "k" * 20, "secret": "s" * 20},
                             headers=headers)
        assert refused.status_code == 422
        assert refused.json()["detail"]["permissions"] == ["enableWithdrawals"]
        assert client.get("/allocations", headers=headers).json()["trade_key"]["linked"] is False
