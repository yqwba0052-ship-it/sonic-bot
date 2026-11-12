"""Kopyon source: history once, then every 10 minutes, no duplicates, budget, no key leaks."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from sonic import kopyon, leader
from sonic.api import create_app
from tests.test_system import build

T = "5141621486582775041"
DAY = 86_400_000


def position(n: int, closed: int, status: str = "CLOSED", pnl: float = 10.0) -> dict:
    return {"positionId": f"p{n}", "symbol": "ZECUSDT", "side": "LONG" if n % 2 else "SHORT",
            "status": status, "leverage": 20, "entryPrice": 1300 + n, "closePrice": 1310 + n,
            "openedAt": closed - 3_600_000, "closedAt": closed, "closedVolume": 10,
            "maxOpenInterest": 10, "grossPnl": pnl, "pnlCurrency": "USDT"}


class FakeKopyon:
    """Newest closed first, 20 a page, like the real API."""

    def __init__(self, positions: list[dict], key_ok: str = "good-kopyon-key-1234") -> None:
        self.positions = positions
        self.key_ok = key_ok
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.headers.get("X-API-KEY") != self.key_ok:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if request.url.path.endswith("/traderProfile"):
            return httpx.Response(200, json={"data": {"traderId": T}})
        assert request.url.params["sortBy"] == "closed"
        ordered = sorted(self.positions, key=lambda p: -p["closedAt"])
        start = int(request.url.params.get("cursor", 0))
        page = ordered[start:start + 20]
        data = {"positions": page, "total": len(ordered)}
        if start + 20 < len(ordered):
            data["nextCursor"] = str(start + 20)
        return httpx.Response(200, json={"data": data})


def test_position_mapping_keeps_what_is_given_and_nothing_else() -> None:
    trade = kopyon.to_trade(position(1, 1_800_000_000_000))
    assert trade["side"] == "BUY" and trade["leverage"] == 20 and trade["qty"] == 10
    assert trade["fee"] is None and trade["pnl"] == 10.0 and trade["source_ref"] == "p1"
    assert kopyon.to_trade(position(2, 1_800_000_000_000, status="PARTIALLY_CLOSED")) is None
    assert kopyon.to_trade({**position(3, 1), "closePrice": None}) is None


def test_interval_widens_to_stay_inside_the_free_plan() -> None:
    assert kopyon.interval_minutes(1) == 10 and kopyon.interval_minutes(2) == 10
    assert kopyon.interval_minutes(3) >= 14  # 3 traders at 10 min would pass the budget
    for n in range(1, 8):
        per_month = n * 30 * 24 * 60 / kopyon.interval_minutes(n)
        assert per_month <= kopyon.BUDGET_30D


async def test_history_once_then_only_new_positions(monkeypatch) -> None:
    engine, _, _, clock = build()
    now = int(clock.now * 1000)
    fake = FakeKopyon([position(n, now - n * DAY, pnl=-5 if n % 7 == 0 else 10)
                       for n in range(1, 46)])
    monkeypatch.setattr(kopyon, "transport", lambda: httpx.MockTransport(fake.handler))
    engine.lab.add(T, "赏你一个甜蜜蜜")
    engine.store.put("kopyon_key", {"key": "good-kopyon-key-1234"})
    report = await engine.sync_kopyon()
    assert report[T]["collected"] == 45 and report[T]["backfilled"]
    assert len(fake.calls) == 3  # 45 positions = 3 pages of 20
    saved = engine.lab.trades(T)
    assert len(saved) == 45 and all(t["source"] == "kopyon" and t["fee"] is None for t in saved)
    # 10 minutes later: one new position closes; one request reads it, nothing twice
    clock.now += 600
    fake.positions.append(position(99, int(clock.now * 1000) - 1000))
    fake.calls.clear()
    report = await engine.sync_kopyon()
    assert report[T]["last_added"] == 1 and len(fake.calls) == 1
    assert len(engine.lab.trades(T)) == 46
    # before the interval: nothing is asked
    fake.calls.clear()
    assert await engine.sync_kopyon() == {}
    assert fake.calls == []
    usage = engine.store.get("kopyon_usage")
    assert sum(usage.values()) == 4


async def test_errors_and_budget_are_reported_not_hidden(monkeypatch) -> None:
    engine, _, _, clock = build()
    fake = FakeKopyon([position(1, int(clock.now * 1000) - DAY)])
    monkeypatch.setattr(kopyon, "transport", lambda: httpx.MockTransport(fake.handler))
    engine.lab.add(T, "x")
    engine.store.put("kopyon_key", {"key": "wrong-key-000000000"})
    report = await engine.sync_kopyon()
    assert report[T]["last_error"] == "kopyon_key_invalid"
    events = [e["code"] for e in engine.store.events(10)]
    assert "kopyon_failed" in events
    engine.store.put("kopyon_key", {"key": "good-kopyon-key-1234"})
    engine.store.put("kopyon_usage", {kopyon.today(int(clock.now * 1000)): kopyon.BUDGET_30D})
    clock.now += 700
    fake.calls.clear()
    report = await engine.sync_kopyon()
    assert report[T]["last_error"] == "kopyon_budget" and fake.calls == []


async def test_only_lead_traders_with_a_binance_id_are_synced(monkeypatch) -> None:
    engine, _, _, clock = build()
    fake = FakeKopyon([])
    monkeypatch.setattr(kopyon, "transport", lambda: httpx.MockTransport(fake.handler))
    engine.lab.add("t-abc123", "by name only")
    engine.lab.add(leader.ACCOUNT_ID, "mine", kind="account")
    engine.lab.add(T, "lead")
    engine.lab.set_status(T, "paused")
    engine.store.put("kopyon_key", {"key": "good-kopyon-key-1234"})
    assert await engine.sync_kopyon() == {}
    assert fake.calls == []


def test_api_key_checked_stored_apart_and_never_shown(monkeypatch) -> None:
    engine, _, _, _ = build()
    fake = FakeKopyon([])
    monkeypatch.setattr(kopyon, "transport", lambda: httpx.MockTransport(fake.handler))
    engine.lab.add(T, "lead")
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        bad = client.put("/kopyon", json={"key": "wrong-key-000000000"}, headers=headers)
        assert bad.status_code == 422 and bad.json()["detail"] == "kopyon_key_invalid"
        ok = client.put("/kopyon", json={"key": "good-kopyon-key-1234"}, headers=headers).json()
        assert ok["linked"] and ok["hint"] == "...1234" and ok["every_minutes"] == 10
        shown = json.dumps(client.get("/kopyon", headers=headers).json())
        listed = json.dumps(client.get("/traders", headers=headers).json())
        events = json.dumps(client.get("/events", headers=headers).json())
        for text in (shown, listed, events):
            assert "good-kopyon-key-1234" not in text
        assert engine.store.get("account_key") is None  # the Binance key is a separate thing
        lead = [t for t in client.get("/traders", headers=headers).json()
                if t["trader_id"] == T][0]
        assert lead["collection"]["kopyon"]["active"] is True
    sent = {r.url.host for r in fake.calls}
    assert sent == {"api.kopyon.com"}
