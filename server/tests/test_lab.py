"""Learning from traders (leader.py) and the read-only account link (account.py)."""

from __future__ import annotations

import json
import math
import random
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from sonic import account, leader
from sonic.api import create_app
from sonic.strategy import BAR_MS, Bar, ema
from tests.test_system import build

T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)


def walk(seed: int, n: int = 2600, start: float = 100.0) -> list[Bar]:
    """Trending random walk: long up and down legs, so EMA crosses happen."""
    rng = random.Random(seed)
    price, bars = start, []
    for i in range(n):
        drift = 0.004 * math.sin(i / 40)
        close = price * math.exp(drift + rng.gauss(0, 0.012))
        high = max(price, close) * (1 + abs(rng.gauss(0, 0.004)))
        low = min(price, close) * (1 - abs(rng.gauss(0, 0.004)))
        bars.append(Bar(T0 + i * BAR_MS, price, high, low, close))
        price = close
    return bars


MARKET = {"BTCUSDT": walk(1, start=30000), "SOLUSDT": walk(2), "XRPUSDT": walk(3, start=0.5)}
for name in leader.SONIC_SYMBOLS:
    MARKET.setdefault(name, walk(hash(name) % 1000))
END = MARKET["BTCUSDT"][-1].t + BAR_MS


async def bars_until(symbol: str, interval: str, end_ms: int, limit: int) -> list[Bar]:
    return [b for b in MARKET[symbol] if b.t < end_ms][-limit:]


def trend_trader(symbol: str) -> list[dict]:
    """A made-up trader: buys when EMA 20 crosses above EMA 100, sells 6 bars later."""
    bars = MARKET[symbol]
    closes = [b.c for b in bars]
    fast, slow = ema(closes, 20), ema(closes, 100)
    out = []
    for i in range(400, len(bars) - 7):
        if fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
            out.append({"symbol": symbol, "side": "BUY", "leverage": 5.0, "qty": 1.0,
                        "entry_price": bars[i].c, "exit_price": bars[i + 6].c,
                        "opened_ms": bars[i].t + BAR_MS, "closed_ms": bars[i + 6].t + BAR_MS,
                        "pnl": bars[i + 6].c - bars[i].c, "fee": None})
    return out


def four_coins() -> list[dict]:
    return [t for s in ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT") for t in trend_trader(s)]


def lab() -> leader.Lab:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return leader.Lab(db, lambda: END)


# ---------- import

def test_template_and_arabic_rows_parse() -> None:
    parsed = leader.parse_csv(leader.TEMPLATE)
    assert parsed["format"] == "positions" and len(parsed["trades"]) == 2
    first = parsed["trades"][0]
    assert first["side"] == "BUY" and first["symbol"] == "BTCUSDT" and first["leverage"] == 10
    text = ("العملة,الاتجاه,سعر الدخول,سعر الخروج,وقت الدخول,وقت الخروج\n"
            "sol,شراء,١٥٠,١٦٠,2026-10-01 08:00,2026-10-02 08:00\n"
            "XRP/USDT,بيع,0.6,,2026-10-03 08:00,\n"          # still open: skipped
            "DOGE,sideways,1,2,2026-10-03 08:00,2026-10-04\n")  # unknown side: error
    parsed = leader.parse_csv(text)
    assert [t["symbol"] for t in parsed["trades"]] == ["SOLUSDT"]
    assert parsed["trades"][0]["entry_price"] == 150 and parsed["trades"][0]["pnl"] is None
    assert parsed["skipped"] == 1 and parsed["errors"] == [[4, "side"]]


def test_unknown_columns_are_reported() -> None:
    parsed = leader.parse_csv("a,b\n1,2\n")
    assert parsed["trades"] == [] and parsed["errors"][0][1].startswith("unknown_columns")


def test_fills_pair_into_positions_including_a_flip() -> None:
    text = ("Date(UTC),Symbol,Side,Price,Quantity,Fee,Realized Profit\n"
            "2026-10-01 00:00:00,SOLUSDT,BUY,100,1,0.05,0\n"
            "2026-10-01 04:00:00,SOLUSDT,BUY,110,1,0.05,0\n"
            "2026-10-02 00:00:00,SOLUSDT,SELL,120,3,0.15,30\n"   # closes 2, opens a short of 1
            "2026-10-03 00:00:00,SOLUSDT,BUY,115,1,0.05,5\n")
    parsed = leader.parse_csv(text)
    assert parsed["format"] == "fills" and parsed["skipped"] == 0
    long_, short = parsed["trades"]
    assert long_["side"] == "BUY" and long_["entry_price"] == pytest.approx(105)
    assert long_["exit_price"] == 120 and long_["qty"] == 2
    assert long_["pnl"] == pytest.approx(30)
    assert short["side"] == "SELL" and short["entry_price"] == 120 and short["exit_price"] == 115


def test_trader_ids_from_links() -> None:
    assert leader.trader_ref(
        "https://www.binance.com/en/copy-trading/lead-details/4123456789?timeRange=30D"
    ) == "4123456789"
    assert leader.trader_ref("https://x.com/a?portfolioId=998877") == "998877"
    assert leader.trader_ref("abc") == "abc"
    assert leader.trader_ref("bad id!") is None


# ---------- traders: add, pause, delete

def test_add_pause_and_delete_after_confirmation() -> None:
    lab_ = lab()
    lab_.add("https://www.binance.com/en/copy-trading/lead-details/4123456789", "Ali")
    assert lab_.traders()[0]["name"] == "Ali" and lab_.traders()[0]["status"] == "watching"
    trades = trend_trader("SOLUSDT")
    assert lab_.import_trades("4123456789", trades, "csv") == len(trades)
    assert lab_.import_trades("4123456789", trades, "csv") == 0  # duplicates ignored
    lab_.set_status("4123456789", "paused")
    assert lab_.trader("4123456789")["status"] == "paused"
    assert len(lab_.trades("4123456789")) == len(trades)  # pause keeps the data
    lab_.add("other", "Other")
    lab_.import_trades("other", trades[:3], "csv")
    lab_.delete("4123456789")
    assert [t["trader_id"] for t in lab_.traders()] == ["other"]  # off the list
    assert lab_.listed("4123456789") is None
    assert len(lab_.trades("4123456789")) == len(trades)  # its trades are kept
    assert len(lab_.trades("other")) == 3
    with pytest.raises(KeyError):
        lab_.import_trades("4123456789", trades, "csv")  # no longer watched
    lab_.add("4123456789", "Ali")  # adding again brings it back with its data
    assert lab_.listed("4123456789")["status"] == "watching"
    assert len(lab_.trades("4123456789")) == len(trades)


# ---------- learning and testing

async def test_study_learns_tests_out_of_sample_and_saves_a_version() -> None:
    lab_ = lab()
    lab_.add("trend1", "Trend")
    trades = four_coins()
    lab_.import_trades("trend1", trades, "csv")
    report = await lab_.study("trend1", bars_until)
    assert report["learn"]["status"] == "ok", report.get("learn")
    model = report["model"]
    assert model["sides"] == ["BUY"] and model["trend"] == "with"
    assert set(model["symbols"]) <= {"SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"}
    test = report["test"]
    for key in ("learned", "sonic"):
        assert {"trades", "win_rate", "net_pnl", "max_drawdown"} <= test[key].keys()
    assert test["trader"]["trades"] > 0
    assert report["learn"]["split_ms"] > trades[0]["opened_ms"]
    assert "ليست استراتيجيته الحقيقية" in report["warning"]
    assert lab_.latest_model("trend1")["id"] == report["model_id"]
    await lab_.study("trend1", bars_until)
    assert len(lab_.models("trend1")) == 2  # every update is a new saved version


async def test_too_few_trades_gives_analysis_only() -> None:
    lab_ = lab()
    lab_.add("few", "")
    lab_.import_trades("few", trend_trader("SOLUSDT")[:5], "csv")
    report = await lab_.study("few", bars_until)
    assert report["learn"]["status"] == "not_enough_trades" and report["model"] is None
    assert report["stats"]["usable"] == 5


async def test_paper_trading_opens_and_closes_without_orders() -> None:
    lab_ = lab()
    lab_.add("trend1", "")
    lab_.import_trades("trend1", four_coins(), "csv")
    await lab_.study("trend1", bars_until)
    model = lab_.latest_model("trend1")["model"]
    model["rsi"], model["dist"] = [0, 100], [-99, 99]  # make a signal easy to get
    lab_.db.execute("UPDATE trader_models SET model = ?", (json.dumps(model),))
    upto = {"n": 2000}

    async def bars(symbol: str) -> list[Bar]:
        return MARKET[symbol][: upto["n"]]

    for _ in range(300):
        await lab_.paper_tick(bars)
        upto["n"] += 1
    summary = lab_.paper_summary("trend1")
    assert summary["trades"] > 0 and summary["win_rate"] is not None
    lab_.set_status("trend1", "paused")
    before = lab_.db.execute("SELECT COUNT(*) FROM trader_paper").fetchone()[0]
    for _ in range(50):
        await lab_.paper_tick(bars)
        upto["n"] += 1
    after = lab_.db.execute("SELECT COUNT(*) FROM trader_paper").fetchone()[0]
    assert after == before  # a paused trader opens no new paper trades


# ---------- read-only account link

def test_only_a_read_only_key_passes() -> None:
    assert account.read_only_problems({"enableReading": True, "ipRestrict": True,
                                       "enableFutures": False}) == []
    assert account.read_only_problems({"enableReading": True, "enableFutures": True,
                                       "enableWithdrawals": True}) == [
        "enableFutures", "enableWithdrawals"]
    assert account.read_only_problems({"enableReading": False}) == ["enableReading_off"]


def mock_binance(permissions: dict, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.host, request.url.path))
        if request.url.path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(200, json=permissions)
        if request.url.path == "/fapi/v1/trade/asyn":
            return httpx.Response(200, json={"downloadId": "777", "avgCostTimestampOfLast30d": 1})
        if request.url.path == "/fapi/v1/trade/asyn/id":
            return httpx.Response(200, json={"downloadId": "777", "status": "completed",
                                             "url": "https://bin.bnbstatic.com/history.csv"})
        if request.url.host == "bin.bnbstatic.com":
            day = 86_400_000
            old = END - 120 * day
            rows = ["Date(UTC),Symbol,Side,Price,Quantity,Fee,Realized Profit",
                    f"{old},XRPUSDT,BUY,0.5,100,0.01,0",
                    f"{old + day},XRPUSDT,SELL,0.55,100,0.01,5"]
            return httpx.Response(200, text=chr(10).join(rows))
        if request.url.path == "/fapi/v2/balance":
            return httpx.Response(200, json=[{"asset": "USDT", "balance": "120.5",
                                              "availableBalance": "80.25"}])
        if request.url.path == "/fapi/v1/income":
            return httpx.Response(200, json=[{"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL"}])
        if request.url.path == "/fapi/v1/userTrades":
            start = int(request.url.params["startTime"])
            if start > END - 8 * 86_400_000:
                return httpx.Response(200, json=[
                    {"symbol": "SOLUSDT", "side": "BUY", "price": "100", "qty": "1",
                     "realizedPnl": "0", "commission": "0.05", "commissionAsset": "USDT",
                     "time": END - 3 * 86_400_000},
                    {"symbol": "SOLUSDT", "side": "SELL", "price": "104", "qty": "1",
                     "realizedPnl": "4", "commission": "0.05", "commissionAsset": "USDT",
                     "time": END - 2 * 86_400_000}])
            return httpx.Response(200, json=[])
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_api_refuses_a_trading_key_and_syncs_a_read_only_one(monkeypatch) -> None:
    engine, _, _, clock = build()
    clock.now = END / 1000
    seen: list = []
    key = {"key": "k" * 20, "secret": "s" * 20}
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        monkeypatch.setattr(account, "transport", lambda: mock_binance(
            {"enableReading": True, "enableFutures": True}, seen))
        refused = client.put("/account", json=key, headers=headers)
        assert refused.status_code == 422
        assert refused.json()["detail"]["permissions"] == ["enableFutures"]
        assert client.get("/account", headers=headers).json()["linked"] is False
        monkeypatch.setattr(account, "transport", lambda: mock_binance(
            {"enableReading": True, "enableFutures": False, "enableWithdrawals": False}, seen))
        linked = client.put("/account", json=key, headers=headers).json()
        assert linked["linked"] and linked["key_hint"].endswith("kkkk")
        assert "secret" not in json.dumps(linked)
        # the first sync starts by itself after linking; a manual one adds nothing twice
        synced = client.post("/account/sync", json={}, headers=headers).json()
        assert synced["trader_id"] == leader.ACCOUNT_ID and synced["added"] in (0, 1)
        recent = [t for t in engine.lab.trades(leader.ACCOUNT_ID) if t["symbol"] == "SOLUSDT"]
        assert len(recent) == 1
        tested = client.post("/account/test", headers=headers).json()
        assert tested["read_only"] and tested["balance"] == {"wallet": 120.5, "available": 80.25}
        assert tested["income_30d"] > 0 and tested["symbols_30d"] == ["SOLUSDT"]
        assert "secret" not in json.dumps(tested) and "s" * 20 not in json.dumps(tested)
        events = json.dumps(client.get("/events", headers=headers).json())
        assert "k" * 20 not in events and "s" * 20 not in events  # keys never in the log
        trade = recent[0]
        assert trade["entry_price"] == 100 and trade["exit_price"] == 104 and trade["pnl"] == 4
    assert {method for method, _, _ in seen} == {"GET"}
    assert {(host, path) for _, host, path in seen} <= {
        ("api.binance.com", "/sapi/v1/account/apiRestrictions"),
        ("fapi.binance.com", "/fapi/v1/income"), ("fapi.binance.com", "/fapi/v1/userTrades"),
        ("fapi.binance.com", "/fapi/v2/balance"), ("fapi.binance.com", "/fapi/v1/trade/asyn"),
        ("fapi.binance.com", "/fapi/v1/trade/asyn/id"), ("bin.bnbstatic.com", "/history.csv")}


def test_reader_cannot_call_any_other_path() -> None:
    reader = account.AccountReader("k" * 20, "s" * 20)

    async def call() -> None:
        async with httpx.AsyncClient() as client:
            await reader._get(client, account.FUTURES, "/fapi/v1/order", {})

    import asyncio

    with pytest.raises(PermissionError):
        asyncio.run(call())


def test_traders_api_add_import_and_confirmed_delete() -> None:
    engine, _, _, _ = build()
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        added = client.post("/traders", json={"ref": "", "name": "Abu Fahad"}, headers=headers)
        assert added.status_code == 200 and added.json()["trader_id"] == "Abu-Fahad"
        arabic = client.post("/traders", json={"ref": "", "name": "أبو فهد"}, headers=headers)
        assert arabic.status_code == 200 and arabic.json()["name"] == "أبو فهد"
        assert arabic.json()["trader_id"].startswith("t-")
        bad = client.post("/traders", json={"ref": "!!", "name": ""}, headers=headers)
        assert bad.status_code == 422
        imported = client.post("/traders/Abu-Fahad/import", json={"csv": leader.TEMPLATE},
                               headers=headers).json()
        assert imported["added"] == 2 and imported["format"] == "positions"
        assert client.get("/traders/Abu-Fahad", headers=headers).json()["trades"] == 2
        assert client.put("/traders/Abu-Fahad", json={"status": "paused"},
                          headers=headers).json()["status"] == "paused"
        assert client.delete("/traders/Abu-Fahad", headers=headers).status_code == 428
        assert client.get("/traders", headers=headers).json()[0]["trades"] == 2
        done = client.delete("/traders/Abu-Fahad?confirm=true", headers=headers).json()
        assert done["removed"]["status"] == "removed"
        listed = [t["name"] for t in client.get("/traders", headers=headers).json()]
        assert listed == ["أبو فهد"]  # only the deleted one left the list
        assert client.get("/traders/Abu-Fahad", headers=headers).status_code == 404
        assert len(engine.lab.trades("Abu-Fahad")) == 2  # kept
        assert client.get("/strategies", headers=headers).status_code == 200
        assert client.get("/traders/template.csv", headers=headers).text.startswith("symbol,")


async def test_a_study_finishing_after_delete_saves_nothing() -> None:
    lab_ = lab()
    lab_.add("gone", "")
    lab_.import_trades("gone", four_coins(), "csv")

    async def slow_bars(symbol: str, interval: str, end_ms: int, limit: int) -> list[Bar]:
        if lab_.trader("gone") is not None and symbol == "BTCUSDT":
            lab_.delete("gone")  # the owner deletes while the study is still reading prices
        return await bars_until(symbol, interval, end_ms, limit)

    with pytest.raises(KeyError):
        await lab_.study("gone", slow_bars)
    assert lab_.db.execute("SELECT COUNT(*) FROM trader_models").fetchone()[0] == 0


# ---------- ranking of saved strategies

def outcome(net: float, dd: float = 0.05, pf: float = 1.8, months_pos: float = 0.75,
           win: float = 0.6, worst: float = -0.012, trades: int = 40, days: int = 300,
           yearly: float | None = None) -> dict:
    return {"trades": trades, "wins": int(trades * win), "win_rate": win, "net_pnl": net,
            "net_pct": net / 30, "yearly_pct": net / 30 if yearly is None else yearly,
            "max_drawdown": dd, "profit_factor": pf, "months": 10,
            "months_positive": months_pos, "worst_trade": worst, "days": days, "open_at_end": 0}


def save(lab_: leader.Lab, trader: str, result: dict, model: dict | None = None) -> int:
    lab_.add(trader, trader)
    report = {"notes": [], "test": {"learned": result, "sonic": outcome(3)}}
    cur = lab_.db.execute(
        "INSERT INTO trader_models (trader_id, created_ms, model, report) VALUES (?, ?, ?, ?)",
        (trader, END, json.dumps(model or {"symbols": []}), json.dumps(report)))
    lab_.db.commit()
    return cur.lastrowid


def test_exclusions_name_the_reason() -> None:
    assert leader.exclusion(outcome(5)) is None
    assert "بيانات غير كافية" in leader.exclusion(outcome(5, trades=8))
    assert "خاسرة" in leader.exclusion(outcome(-1))
    assert "تراجع كبير" in leader.exclusion(outcome(5, dd=0.35))
    assert "خسارة كبيرة" in leader.exclusion(outcome(5, worst=-0.06))
    assert "معامل ربح" in leader.exclusion(outcome(5, pf=1.05))
    assert "غير مستقرة" in leader.exclusion(outcome(5, months_pos=0.3))
    assert "اختبار قديم" in leader.exclusion({"trades": 40, "net_pnl": 5})


def test_a_high_win_rate_alone_does_not_win() -> None:
    steady = leader.score(outcome(9, dd=0.04, pf=2.2, months_pos=0.85, win=0.55, yearly=0.3))[0]
    lucky = leader.score(outcome(2, dd=0.15, pf=1.25, months_pos=0.55, win=0.95, yearly=0.05))[0]
    assert steady > lucky


def hold_trader(symbol: str, hold: int) -> list[dict]:
    """Same entries as trend_trader, a different exit: the bars held."""
    out = []
    for t in trend_trader(symbol):
        bars = MARKET[symbol]
        i = next(k for k, b in enumerate(bars) if b.t + BAR_MS == t["opened_ms"])
        j = min(i + hold, len(bars) - 1)
        out.append({**t, "exit_price": bars[j].c, "closed_ms": bars[j].t + BAR_MS,
                    "pnl": bars[j].c - bars[i].c})
    return out


async def test_strategies_on_the_same_coin_are_compared_over_the_same_period() -> None:
    lab_ = lab()
    lab_.add("aaa", "A")
    lab_.import_trades("aaa", four_coins(), "csv")
    lab_.add("bbb", "B")
    lab_.import_trades("bbb", [t for s in ("SOLUSDT", "DOGEUSDT", "ADAUSDT", "XRPUSDT")
                               for t in hold_trader(s, 12)], "csv")
    await lab_.study("aaa", bars_until)
    await lab_.study("bbb", bars_until)
    lab_.delete("bbb")  # a deleted trader's strategy still competes
    state = await lab_.compare_coins(bars_until)
    shared = [c for c in state["coins"].values() if c["strategies"] == 2]
    assert shared, state["coins"].keys()
    coin = shared[0]
    names = {e["trader_id"] for e in coin["ranked"] + coin["excluded"]}
    assert names == {"aaa", "bbb"}
    if coin["same_period"]:
        windows = {(e["from_ms"], e["to_ms"]) for e in coin["ranked"] + coin["excluded"]}
        assert len(windows) == 1  # same market for both
    for e in coin["excluded"]:
        assert e["why"]  # every strategy left out says why
    if coin["best"]:
        assert coin["best"]["reasons"]
    view = lab_.ranking()
    assert view["total"] == 2 and view["coins"].keys() == state["coins"].keys()


def test_a_single_strategy_is_judged_alone_and_approved_ones_are_kept() -> None:
    lab_ = lab()
    good = outcome(12, dd=0.03, pf=2.5, months_pos=0.9, yearly=0.4)
    alone = leader.rank([{"trader_id": "aaa", "name": "A", "status": "watching",
                          "model_id": 1, "result": good}])
    assert alone["best"]["model_id"] == 1 and alone["best"]["reasons"]
    weak = leader.rank([{"trader_id": "aaa", "name": "A", "status": "watching",
                         "model_id": 1, "result": outcome(5, trades=4)}])
    assert weak["best"] is None and "بيانات غير كافية" in weak["excluded"][0]["why"]
    lab_.put_state("coins", {"made_ms": END, "coins": {"ZECUSDT": {
        "symbol": "ZECUSDT", "strategies": 2, "same_period": True, "from_ms": 1, "to_ms": 2,
        "ranked": [], "excluded": [],
        "best": {"trader_id": "aaa", "model_id": 7, "name": "A", "score": 70, "reasons": []}}}})
    view = lab_.ranking(approved={"ZECUSDT": 3})
    zec = view["coins"]["ZECUSDT"]
    assert zec["approved_model"] == 3 and zec["better_than_approved"] is True
    same = lab_.ranking(approved={"ZECUSDT": 7})["coins"]["ZECUSDT"]
    assert same["better_than_approved"] is False


async def test_best_per_coin_paper_trades_that_coin_only() -> None:
    lab_ = lab()
    lab_.add("trend1", "")
    lab_.import_trades("trend1", four_coins(), "csv")
    await lab_.study("trend1", bars_until)
    latest = lab_.latest_model("trend1")
    model = latest["model"]
    model["rsi"], model["dist"], model["age"] = [0, 100], [-99, 99], [0, 500]
    lab_.db.execute("UPDATE trader_models SET model = ?", (json.dumps(model),))
    lab_.delete("trend1")  # its own paper trading stops; the coin's best track does not
    lab_.put_state("coins", {"made_ms": END, "coins": {"SOLUSDT": {
        "best": {"trader_id": "trend1", "model_id": latest["id"], "name": "t", "score": 50,
                 "reasons": []}}}})
    upto = {"n": 2000}

    async def bars(symbol: str) -> list[Bar]:
        return MARKET[symbol][: upto["n"]]

    for _ in range(300):
        await lab_.paper_tick(bars)
        upto["n"] += 1
    symbols = {r[0] for r in lab_.db.execute("SELECT symbol FROM champion_paper")}
    assert symbols == {"SOLUSDT"}
    assert lab_.db.execute("SELECT COUNT(*) FROM trader_paper").fetchone()[0] == 0


async def test_account_sync_runs_every_six_hours_in_the_background(monkeypatch) -> None:
    engine, _, _, clock = build()
    engine.store.put("account_key", {"key": "k" * 20, "secret": "s" * 20})
    calls: list = []

    async def fake_sync(trader_id):
        calls.append(engine.now_ms())
        engine.store.put("account_synced_ms", engine.now_ms())
        return {}

    monkeypatch.setattr(engine, "sync_account", fake_sync)
    engine.maybe_sync_account()
    await engine.sync_task
    engine.maybe_sync_account()  # just synced: nothing new
    assert len(calls) == 1
    clock.now += 5 * 3600
    engine.maybe_sync_account()
    assert engine.sync_task.done() and len(calls) == 1
    clock.now += 3600
    engine.maybe_sync_account()
    await engine.sync_task
    assert len(calls) == 2


def test_live_switch_is_off_unless_the_owner_sets_it(monkeypatch) -> None:
    from sonic import capital

    monkeypatch.delenv("SONIC_LIVE_TRADING", raising=False)
    assert capital.live_allowed() is False
    monkeypatch.setenv("SONIC_LIVE_TRADING", "yes")
    assert capital.live_allowed() is True


def test_key_from_env_file_wins_and_cannot_be_unlinked_from_the_app(monkeypatch) -> None:
    engine, _, _, _ = build()
    monkeypatch.setenv("BINANCE_READ_KEY", "e" * 20)
    monkeypatch.setenv("BINANCE_READ_SECRET", "f" * 20)
    engine.store.put("account_key", {"key": "k" * 20, "secret": "s" * 20})
    assert engine.account_key()["key"] == "e" * 20
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        status = client.get("/account", headers=headers).json()
        assert status["source"] == "env" and status["key_hint"] == "...eeee"
        assert "f" * 20 not in json.dumps(status)
        assert client.delete("/account", headers=headers).status_code == 409


async def test_command_line_check_refuses_a_trading_key(monkeypatch) -> None:
    from sonic import check_account

    monkeypatch.setattr(account, "transport", lambda: mock_binance(
        {"enableReading": True, "enableFutures": True}, []))
    lines, ok = await check_account.check("k" * 20, "s" * 20)
    assert not ok and "enableFutures" in lines[1] and "k" * 20 not in "\n".join(lines)
    monkeypatch.setattr(account, "transport", lambda: mock_binance(
        {"enableReading": True, "ipRestrict": True}, []))
    lines, ok = await check_account.check("k" * 20, "s" * 20)
    assert ok and any("80.25" in line for line in lines) and any("IPs" in line for line in lines)


OWNER_CSV = """symbol,side,leverage,qty,entry_price,exit_price,opened_at,closed_at,pnl,fee
ZECUSDT,SHORT,20,0.021,1270.91,1254.28,,,0.33038772,
ZECUSDT,LONG,20,0.01,1220.64,1236.08,,,0.1421164,
ETHUSDT,SHORT,20,0.009,2293.48,2285.69,,,0.04941331,
ZECUSDT,LONG,20,0.004,1316.87,1328.02,,,0.03931022,
ZECUSDT,LONG,20,0.005,1306.84,1315.0,,,0.0342454,
ZECUSDT,LONG,20,0.004,1318.57,1322.26,,,0.00947834,
ZECUSDT,LONG,20,0.004,1320.47,1326.59,,,0.01892201,
"""


async def test_rows_without_times_become_drafts_and_are_never_learned_from() -> None:
    parsed = leader.parse_csv(OWNER_CSV)
    assert parsed["trades"] == [] and len(parsed["drafts"]) == 7 and parsed["errors"] == []
    assert parsed["rows"][0] == {"line": 2, "status": "draft",
                                 "missing": ["opened_at", "closed_at", "fee"]}
    first = parsed["drafts"][0]
    assert first["side"] == "SELL" and first["opened_ms"] is None and first["fee"] is None
    lab_ = lab()
    lab_.add("5141621486582775041", "trader")
    assert lab_.save_drafts("5141621486582775041", parsed["drafts"]) == 7
    assert lab_.save_drafts("5141621486582775041", parsed["drafts"]) == 0  # no duplicates
    assert lab_.traders()[0]["drafts"] == 7 and lab_.traders()[0]["trades"] == 0
    with pytest.raises(ValueError, match="no_trades"):
        await lab_.study("5141621486582775041", bars_until)  # drafts are not learned from
    draft = lab_.drafts("5141621486582775041")[0]
    with pytest.raises(ValueError, match="draft_times"):
        lab_.complete_draft("5141621486582775041", draft["id"], None, None, None)
    done = lab_.complete_draft("5141621486582775041", draft["id"],
                               leader.moment("2026-11-01 10:00"),
                               leader.moment("2026-11-01 14:00"), None)
    assert done["added"] == 1 and len(lab_.drafts("5141621486582775041")) == 6
    trade = lab_.trades("5141621486582775041")[0]
    assert trade["fee"] is None and trade["leverage"] == 20  # the fee stays unknown
    # importing the same trades again with their times replaces the drafts
    full = OWNER_CSV.replace(",,,0.1421164,", ",2026-11-02 08:00,2026-11-02 09:00,0.1421164,")
    again = leader.parse_csv(full)
    assert len(again["trades"]) == 1 and again["rows"][1]["status"] == "saved"
    assert again["rows"][1]["missing"] == ["fee"]
    assert lab_.import_trades("5141621486582775041", again["trades"], "csv") == 1
    assert len(lab_.drafts("5141621486582775041")) == 5  # its draft was completed by it
    assert lab_.import_trades("5141621486582775041", again["trades"], "csv") == 0  # no twice


def test_binance_position_history_columns() -> None:
    text = ("Symbol,Direction,Opened,Closed,Avg. Entry Price,Avg. Close Price,Closing PnL\n"
            "ZECUSDT Perpetual,Long,2026-11-01 10:00:00,2026-11-01 12:30:00,1300,1310,0.05\n")
    parsed = leader.parse_csv(text)
    assert parsed["format"] == "positions" and len(parsed["trades"]) == 1
    trade = parsed["trades"][0]
    assert trade["symbol"] == "ZECUSDT" and trade["side"] == "BUY" and trade["pnl"] == 0.05
    assert parsed["rows"][0]["missing"] == ["leverage", "qty", "fee"]


def test_account_history_reaches_six_months_through_the_official_download(monkeypatch) -> None:
    engine, _, _, clock = build()
    clock.now = END / 1000
    headers = {"Authorization": "Bearer secret"}
    key = {"key": "k" * 20, "secret": "s" * 20}
    with TestClient(create_app(engine, "secret", background=False)) as client:
        monkeypatch.setattr(account, "transport", lambda: mock_binance(
            {"enableReading": True}, []))
        client.put("/account", json=key, headers=headers)
        first = client.post("/account/sync", json={}, headers=headers).json()
        assert first["older"]["status"] in ("requested", "done")
        second = client.post("/account/sync", json={}, headers=headers).json()
        assert second["older"]["status"] == "done"
        symbols = {t["symbol"] for t in engine.lab.trades(leader.ACCOUNT_ID)}
        assert symbols == {"SOLUSDT", "XRPUSDT"}  # recent (API) + months 4-6 (file)
        old = [t for t in engine.lab.trades(leader.ACCOUNT_ID) if t["symbol"] == "XRPUSDT"][0]
        assert old["pnl"] == 5 and old["opened_ms"] < END - 90 * 86_400_000


async def test_download_only_from_binance_file_hosts() -> None:
    reader = account.AccountReader("k" * 20, "s" * 20)
    with pytest.raises(PermissionError):
        await reader.download("https://evil.example.com/x.csv")
    with pytest.raises(PermissionError):
        await reader.download("http://bin.bnbstatic.com/x.csv")


def test_times_are_converted_to_utc() -> None:
    riyadh = 180  # UTC+3
    local = leader.parse_csv(
        "symbol,side,entry_price,exit_price,opened_at,closed_at\n"
        "SOLUSDT,long,100,101,2026-11-10 15:00,2026-11-10 18:00\n", riyadh)
    assert local["utc_offset"] == 180
    assert local["trades"][0]["opened_ms"] == leader.moment("2026-11-10 12:00")
    binance = leader.parse_csv(
        "Symbol,Side,Avg. Entry Price,Avg. Close Price,Opened (UTC),Closed (UTC)\n"
        "SOLUSDT,long,100,101,2026-11-10 15:00,2026-11-10 18:00\n", riyadh)
    assert binance["utc_offset"] == 0  # the file says UTC: kept as is
    assert binance["trades"][0]["opened_ms"] == leader.moment("2026-11-10 15:00")
    assert leader.moment("2026-11-10T15:00:00+03:00", 0) == leader.moment("2026-11-10 12:00")


SCREEN = """ZECUSDT Perpetual
Long 20x Isolated
Opened 2026-11-10 15:20:05
Closed 2026-11-10 19:41:10
Entry Price 1220.64
Avg. Close Price 1236.08
Closing PNL +0.1421 USDT
Max Open Interest 0.010 ZEC
ETHUSDT Perpetual
Short 20x
Entry Price 2293.48
Avg. Close Price 2285.69
Closing PNL +0.0494 USDT
"""


def test_screenshot_text_gives_trades_and_drafts_without_guessing() -> None:
    parsed = leader.parse_screenshot(SCREEN, 180)
    trade = parsed["trades"][0]
    assert trade["symbol"] == "ZECUSDT" and trade["side"] == "BUY" and trade["leverage"] == 20
    assert trade["entry_price"] == 1220.64 and trade["exit_price"] == 1236.08
    assert trade["pnl"] == 0.1421 and trade["qty"] == 0.01 and trade["fee"] is None
    assert trade["opened_ms"] == leader.moment("2026-11-10 12:20:05")  # phone time -> UTC
    draft = parsed["drafts"][0]  # no times on screen: a draft, nothing invented
    assert draft["symbol"] == "ETHUSDT" and draft["side"] == "SELL"
    assert draft["missing"][:2] == ["opened_at", "closed_at"]
    assert parsed["rows"][0]["missing"] == ["fee"]


def test_a_screenshot_completes_the_matching_draft_without_duplicates() -> None:
    lab_ = lab()
    lab_.add("5141621486582775041", "trader")
    lab_.save_drafts("5141621486582775041", leader.parse_csv(OWNER_CSV)["drafts"])
    shot = leader.parse_screenshot(SCREEN, 180)
    assert lab_.import_trades("5141621486582775041", shot["trades"], "screenshot") == 1
    trade = lab_.trades("5141621486582775041")[0]
    assert trade["qty"] == 0.01 and trade["leverage"] == 20 and trade["pnl"] == 0.1421
    assert len(lab_.drafts("5141621486582775041")) == 6  # the ZEC 1220.64 draft is done
    again = dict(shot["trades"][0], opened_ms=shot["trades"][0]["opened_ms"] + 30_000)
    assert lab_.import_trades("5141621486582775041", [again], "csv") == 0  # same position
    assert lab_.save_drafts("5141621486582775041", shot["drafts"]) == 0  # ETH draft exists


async def test_study_starts_by_itself_at_twenty_complete_trades(monkeypatch) -> None:
    engine, _, _, _ = build()
    engine.lab.add("auto", "")
    started: list = []

    async def fake_study(trader_id, bars_until):
        started.append(trader_id)
        return {}

    async def fake_compare(bars_until):
        return {}

    monkeypatch.setattr(engine.lab, "study", fake_study)
    monkeypatch.setattr(engine.lab, "compare_coins", fake_compare)
    engine.lab.import_trades("auto", four_coins()[:19], "csv")
    assert engine.queue_study("auto") is False  # 19: not yet
    engine.lab.import_trades("auto", four_coins()[19:25], "csv")
    assert engine.queue_study("auto") is True
    await engine.study_tasks["auto"]
    assert started == ["auto"]
