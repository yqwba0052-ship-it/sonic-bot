"""Fast desk inside Sonic: /fast route, isolation, and the traders-lab switch."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from test_system import Clock, FakeMarket

from sonic.api import create_app
from sonic.config import Settings
from sonic.engine import Engine
from sonic.exchange import PaperExchange
from sonic.fastdesk import START_VARIANT
from sonic.store import Store

AUTH = {"Authorization": "Bearer secret"}


def build(tmp_path, monkeypatch, desk: bool, trader_lab: bool = True) -> Engine:
    monkeypatch.delenv("SONIC_FAST_THRESHOLDS", raising=False)
    if desk:
        thr = tmp_path / "thresholds.json"
        thr.write_text(json.dumps({"symbols": {"SOLUSDT": {"days": [], "thresholds": {
            "60": {"0.01": {"rise": 0.002, "buyshare30": 0.9}}}}}}), encoding="utf-8")
        monkeypatch.setenv("SONIC_FAST_THRESHOLDS", str(thr))
    db = str(tmp_path / "sonic.db")
    store = Store(db)
    clock = Clock()
    market = FakeMarket()
    market.clock = clock
    settings = Settings(mode="paper", token="secret", db_path=db, trader_lab=trader_lab)
    return Engine(store, PaperExchange(market, store, 10000.0), settings, clock=clock)


def test_fast_route_off_without_env(tmp_path, monkeypatch) -> None:
    engine = build(tmp_path, monkeypatch, desk=False)
    client = TestClient(create_app(engine, "secret", background=False))
    assert client.get("/fast", headers=AUTH).json() == {"enabled": False}
    assert client.get("/status", headers=AUTH).status_code == 200


def test_fast_route_needs_token(tmp_path, monkeypatch) -> None:
    engine = build(tmp_path, monkeypatch, desk=True)
    client = TestClient(create_app(engine, "secret", background=False))
    assert client.get("/fast").status_code == 401


def test_fast_route_with_desk(tmp_path, monkeypatch) -> None:
    engine = build(tmp_path, monkeypatch, desk=True)
    client = TestClient(create_app(engine, "secret", background=False))
    body = client.get("/fast", headers=AUTH).json()
    assert body["enabled"] and body["variant"] == START_VARIANT
    assert body["balance"] == 30.0 and body["leverage"] == 1
    assert body["mind"]["mode"] == "shadow"


async def test_desk_exception_does_not_stop_sonic(tmp_path, monkeypatch) -> None:
    engine = build(tmp_path, monkeypatch, desk=True)

    def boom(*_: object) -> list:
        raise RuntimeError("bad message")

    monkeypatch.setattr(engine.fast, "on_trade", boom)
    msg = json.dumps({"data": {"s": "SOLUSDT", "T": 1, "p": "1", "q": "1", "m": False}})
    await engine.fast.handle(msg)
    await engine.fast.handle("not json")
    codes = [r[0] for r in engine.fast.db.execute("SELECT code FROM fast_events")]
    assert codes == ["desk_error", "desk_error"]
    assert engine.halted == ""
    client = TestClient(create_app(engine, "secret", background=False))
    assert client.get("/status", headers=AUTH).status_code == 200


def test_trader_lab_switch_off_stops_studies(tmp_path, monkeypatch) -> None:
    engine = build(tmp_path, monkeypatch, desk=False, trader_lab=False)
    monkeypatch.setattr(engine.lab, "needs_study", lambda _tid: True)
    assert engine.queue_study("anyone") is False
    assert Settings(mode="paper", token="t", db_path=":memory:").trader_lab is True  # default
