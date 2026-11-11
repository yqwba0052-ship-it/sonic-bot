"""HTTP API for the phone app. Every route except /health needs the bearer token."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi import Path as Path_
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import account, capital, leader
from .engine import Engine
from .risk import ALLOWED_SYMBOLS, Limits


def create_app(engine: Engine, token: str, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(engine.run_forever()) if background else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def require_token(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(status_code=401, detail="bad_token")

    app = FastAPI(title="Sonic bot", lifespan=lifespan)
    secured = [Depends(require_token)]

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/status", dependencies=secured)
    async def status() -> dict[str, Any]:
        return engine.status()

    @app.get("/trades", dependencies=secured)
    async def trades(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return engine.store.trades(limit)

    @app.get("/events", dependencies=secured)
    async def events(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return engine.store.events(limit)

    @app.put("/limits", dependencies=secured)
    async def put_limits(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        try:
            limits = Limits.model_validate(body)
        except ValidationError as error:
            text = str(error)
            code = next(
                (name for name in ("daily_loss_above_capital", "unknown_symbol") if name in text),
                "invalid_values",
            )
            raise HTTPException(status_code=422, detail=code) from error
        too_small = await engine.unaffordable(limits)
        if too_small:
            raise HTTPException(status_code=422, detail="coin_below_minimum")
        try:
            await engine.set_limits(limits)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return limits.model_dump()

    @app.put("/settings", dependencies=secured)
    async def put_settings(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        known = {"keep_awake", "auto_resume"}
        if not body or set(body) - known or not all(isinstance(v, bool) for v in body.values()):
            raise HTTPException(status_code=422, detail="invalid_values")
        if "keep_awake" in body:
            await engine.set_keep_awake(body["keep_awake"])
        if "auto_resume" in body:
            await engine.set_auto_resume(body["auto_resume"])
        return engine.status()

    @app.get("/chart", dependencies=secured)
    async def chart_data(
        symbol: str = Query(pattern=r"^[A-Z0-9]{2,20}USDT$"),
        source: str = Query(default="live", pattern=r"^(live|replay)$"),
        trade_id: int | None = Query(default=None, ge=1),
        bars: int = Query(default=120, ge=30, le=600),
    ) -> dict[str, Any]:
        """4-hour candles, the bot's EMAs and its trades for the chart screen."""
        if symbol not in ALLOWED_SYMBOLS:
            raise HTTPException(status_code=422, detail="unknown_symbol")
        return await engine.chart(symbol, source, trade_id, bars)

    @app.get("/simulate", dependencies=secured)
    async def simulate(
        symbol: str = Query(pattern=r"^[A-Z0-9]{2,20}USDT$"),
        source: str = Query(default="live", pattern=r"^(live|replay)$"),
        months: int = Query(default=1, ge=1, le=6),
        end_ms: int | None = Query(default=None, ge=0),
    ) -> dict[str, Any]:
        """A period of candles and the bot's trades, for the simulator to replay."""
        if symbol not in ALLOWED_SYMBOLS:
            raise HTTPException(status_code=422, detail="unknown_symbol")
        return await engine.simulate(symbol, source, months, end_ms)

    @app.get("/replay/trades", dependencies=secured)
    async def replay_trades(
        symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{2,20}USDT$"),
    ) -> dict[str, Any]:
        """Trades of the last engine replay on past prices."""
        return engine.replay_list(symbol)

    @app.get("/report", dependencies=secured)
    async def report(month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")) -> dict:
        """One month of closed trades against the 80-90% winning-trades goal."""
        return engine.month_report(month)

    @app.get("/insights", dependencies=secured)
    async def insights() -> dict[str, Any]:
        """What the bot learned from its own closed trades."""
        return engine.insights()

    @app.get("/markets", dependencies=secured)
    async def markets() -> list[dict[str, Any]]:
        """Each coin the owner can pick, with the smallest order Binance accepts for it."""
        return await engine.markets()

    @app.post("/start", dependencies=secured)
    async def start() -> dict[str, Any]:
        try:
            await engine.start()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return engine.status()

    @app.post("/stop", dependencies=secured)
    async def stop() -> dict[str, Any]:
        await engine.stop()
        return engine.status()

    @app.post("/panic", dependencies=secured)
    async def panic() -> dict[str, Any]:
        await engine.panic()
        return engine.status()

    @app.post("/clear-halt", dependencies=secured)
    async def clear_halt() -> dict[str, Any]:
        try:
            await engine.clear_halt()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return engine.status()

    apk = Path(__file__).resolve().parent.parent / "downloads" / "sonic.apk"

    # ---------- learning from traders the owner added (leader.py), paper only

    trader_path = r"^[A-Za-z0-9_-]{3,40}$"

    def lab_error(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(status_code=404, detail="unknown_trader")
        return HTTPException(status_code=422, detail=str(error))

    @app.get("/traders", dependencies=secured)
    async def traders() -> list[dict[str, Any]]:
        return engine.lab.traders()

    @app.get("/traders/template.csv", dependencies=secured, response_class=PlainTextResponse)
    async def trader_template() -> str:
        return leader.TEMPLATE

    @app.post("/traders", dependencies=secured)
    async def add_trader(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        ref, name = str(body.get("ref", ""))[:300], str(body.get("name", ""))[:60]
        if not ref.strip() and name.strip():
            # added by name only: a Latin name becomes the id; any other name (Arabic...) gets
            # a short generated id, and the name is shown as written
            slug = "-".join(name.split())
            ref = slug if leader.TRADER_ID.fullmatch(slug) else f"t-{uuid.uuid4().hex[:10]}"
        try:
            return engine.lab.add(ref, name)
        except ValueError as error:
            raise lab_error(error) from error

    @app.get("/traders/{trader_id}", dependencies=secured)
    async def trader_detail(trader_id: str = Path_(pattern=trader_path)) -> dict[str, Any]:
        trader = engine.lab.listed(trader_id)
        if trader is None:
            raise HTTPException(status_code=404, detail="unknown_trader")
        trades = engine.lab.trades(trader_id)
        return {
            "trader": trader, "trades": len(trades), "recent": trades[-20:][::-1],
            "latest": engine.lab.latest_model(trader_id),
            "versions": engine.lab.models(trader_id),
            "paper": engine.lab.paper_summary(trader_id),
        }

    @app.put("/traders/{trader_id}", dependencies=secured)
    async def trader_status(
        body: Annotated[dict[str, Any], Body()], trader_id: str = Path_(pattern=trader_path)
    ) -> dict[str, Any]:
        try:
            return engine.lab.set_status(trader_id, str(body.get("status")))
        except (KeyError, ValueError) as error:
            raise lab_error(error) from error

    @app.delete("/traders/{trader_id}", dependencies=secured)
    async def delete_trader(
        trader_id: str = Path_(pattern=trader_path), confirm: bool = Query(default=False)
    ) -> dict[str, Any]:
        if not confirm:  # the app sends confirm=true only after "are you sure?" -> OK
            raise HTTPException(status_code=428, detail="confirm_required")
        try:  # off the list and no longer watched; trades and strategies stay saved
            return {"removed": engine.lab.delete(trader_id)}
        except KeyError as error:
            raise lab_error(error) from error

    @app.post("/traders/{trader_id}/import", dependencies=secured)
    async def import_trades(
        body: Annotated[dict[str, Any], Body()], trader_id: str = Path_(pattern=trader_path)
    ) -> dict[str, Any]:
        text = str(body.get("csv", ""))
        if len(text) > 5_000_000:
            raise HTTPException(status_code=413, detail="too_large")
        parsed = leader.parse_csv(text)
        try:
            added = engine.lab.import_trades(trader_id, parsed["trades"], "csv")
        except KeyError as error:
            raise lab_error(error) from error
        return {"format": parsed["format"], "read": len(parsed["trades"]), "added": added,
                "still_open": parsed["skipped"], "errors": parsed["errors"][:50]}

    @app.get("/strategies", dependencies=secured)
    async def strategies() -> dict[str, Any]:
        """The best strategy per coin, compared out of sample over the same period, with
        reasons; deleted traders' strategies included. Approved ones are never swapped."""
        return engine.lab.ranking(engine.capital.approved_by_coin())

    # ---------- money for approved strategies: approve -> allocate -> activate (capital.py)

    def cap_error(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(status_code=404, detail=str(error).strip("'"))
        return HTTPException(status_code=422, detail=str(error))

    def trade_key_view() -> dict[str, Any]:
        saved = engine.store.get("trade_key")
        return {"linked": bool(saved), "hint": ("..." + saved["key"][-4:]) if saved else None,
                "checked_ms": saved.get("checked_ms") if saved else None}

    @app.get("/allocations", dependencies=secured)
    async def allocations() -> dict[str, Any]:
        return {"items": engine.capital.views(), "trade_key": trade_key_view(),
                "live_allowed": capital.live_allowed(), "phrase": capital.LIVE_PHRASE}

    @app.post("/allocations/{model_id}/approve", dependencies=secured)
    async def approve_strategy(model_id: int) -> dict[str, Any]:
        try:
            return engine.capital.approve(model_id)
        except (KeyError, ValueError) as error:
            raise cap_error(error) from error

    async def bounds_for(model_id: int) -> dict[str, Any]:
        saved = engine.store.get("account_key")
        if not saved:
            raise ValueError("no_account_key")  # the balance is read with the read-only key
        row = engine.capital.row(model_id)
        if row is None:
            raise KeyError("not_approved")
        model_row = engine.lab.db.execute("SELECT model FROM trader_models WHERE id = ?",
                                          (model_id,)).fetchone()
        model = json.loads(model_row["model"])
        reader = account.AccountReader(saved["key"], saved["secret"], engine.now_ms)
        money = await reader.balance()
        minimums = await capital.coin_minimums(engine.exchange, model["symbols"], model,
                                               int(max(1, min(5, row["leverage"]))))
        return engine.capital.bounds(model_id, money["wallet"], money["available"], minimums)

    @app.get("/allocations/{model_id}/bounds", dependencies=secured)
    async def allocation_bounds(model_id: int) -> dict[str, Any]:
        try:
            return await bounds_for(model_id)
        except (KeyError, ValueError) as error:
            raise cap_error(error) from error
        except account.NotReadOnly as error:
            raise HTTPException(status_code=422, detail="key_not_read_only") from error

    @app.put("/allocations/{model_id}", dependencies=secured)
    async def allocate(model_id: int, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        try:
            amount = float(body.get("amount"))
            return engine.capital.allocate(model_id, amount, await bounds_for(model_id))
        except (TypeError, ValueError, KeyError) as error:
            raise cap_error(error) from error

    @app.put("/allocations/{model_id}/limits", dependencies=secured)
    async def allocation_limits(
        model_id: int, body: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        try:
            return engine.capital.set_limits(model_id, float(body.get("max_loss")),
                                             float(body.get("daily_loss")))
        except (TypeError, ValueError, KeyError) as error:
            raise cap_error(error) from error

    @app.post("/allocations/{model_id}/stop", dependencies=secured)
    async def stop_strategy(model_id: int, body: Annotated[dict[str, Any], Body()]) -> dict:
        try:
            return engine.capital.stop(model_id, bool(body.get("close_open")))
        except KeyError as error:
            raise cap_error(error) from error

    @app.post("/allocations/{model_id}/activate", dependencies=secured)
    async def activate_strategy(
        model_id: int, body: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        saved = engine.store.get("trade_key")
        try:
            return engine.capital.activate(model_id, str(body.get("mode")),
                                           str(body.get("phrase", "")), saved)
        except (KeyError, ValueError) as error:
            raise cap_error(error) from error

    @app.get("/allocations/{model_id}/gate", dependencies=secured)
    async def strategy_gate(model_id: int, mode: str = Query(default="demo")) -> list[dict]:
        return engine.capital.gate(model_id, engine.store.get("trade_key"), mode)

    @app.post("/allocations/emergency", dependencies=secured)
    async def strategies_emergency() -> dict[str, Any]:
        closed = await engine.capital.emergency(engine.strategy_client, engine.log)
        return {"closed": closed}

    @app.put("/trade-key", dependencies=secured)
    async def link_trade_key(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        key, secret = str(body.get("key", "")).strip(), str(body.get("secret", "")).strip()
        if not (16 <= len(key) <= 128 and 16 <= len(secret) <= 128):
            raise HTTPException(status_code=422, detail="invalid_values")
        try:
            permissions = await account.AccountReader(key, secret, engine.now_ms).permissions()
        except Exception as error:  # noqa: BLE001 - wrong key, network, region
            raise HTTPException(status_code=502, detail="key_check_failed") from error
        problems = capital.trade_key_problems(permissions)
        if problems:
            raise HTTPException(status_code=422, detail={
                "code": "trade_key_not_safe", "permissions": problems})
        engine.store.put("trade_key", {"key": key, "secret": secret,
                                       "checked_ms": engine.now_ms(), "problems": []})
        engine.strategy_clients.clear()
        engine.log("info", "trade_key_linked", "futures only, " + key[-4:])
        return trade_key_view()

    @app.delete("/trade-key", dependencies=secured)
    async def unlink_trade_key() -> dict[str, Any]:
        engine.store.put("trade_key", None)
        engine.strategy_clients.clear()
        return trade_key_view()

    @app.post("/traders/{trader_id}/study", dependencies=secured)
    async def study_trader(trader_id: str = Path_(pattern=trader_path)) -> dict[str, Any]:
        if engine.lab.listed(trader_id) is None:
            raise HTTPException(status_code=404, detail="unknown_trader")
        try:
            report = await engine.lab.study(trader_id, engine.exchange.bars_until)
        except (KeyError, ValueError) as error:
            raise lab_error(error) from error
        # new test results: compare again, coin by coin (a failure here keeps the report)
        with suppress(Exception):
            await engine.lab.compare_coins(engine.exchange.bars_until)
        return report

    @app.post("/traders/{trader_id}/claim", dependencies=secured)
    async def claim_trades(
        body: Annotated[dict[str, Any], Body()], trader_id: str = Path_(pattern=trader_path)
    ) -> dict[str, Any]:
        """Move the owner's synced trades in a time range to this trader (owner's statement)."""
        try:
            start, end = int(body["from_ms"]), int(body["to_ms"])
            moved = engine.lab.move_trades(leader.ACCOUNT_ID, trader_id, start, end)
        except (KeyError, ValueError, TypeError) as error:
            raise lab_error(error) from error
        return {"moved": moved}

    @app.get("/account", dependencies=secured)
    async def account_status() -> dict[str, Any]:
        saved = engine.store.get("account_key")
        return {
            "linked": bool(saved),
            "key_hint": ("..." + saved["key"][-4:]) if saved else None,
            "checked_ms": saved.get("checked_ms") if saved else None,
            "synced_ms": engine.store.get("account_synced_ms"),
            "target": engine.store.get("account_target"),
        }

    @app.put("/account", dependencies=secured)
    async def link_account(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        key, secret = str(body.get("key", "")).strip(), str(body.get("secret", "")).strip()
        if not (16 <= len(key) <= 128 and 16 <= len(secret) <= 128):
            raise HTTPException(status_code=422, detail="invalid_values")
        reader = account.AccountReader(key, secret, engine.now_ms)
        try:
            await reader.check()
        except account.NotReadOnly as error:
            raise HTTPException(status_code=422, detail={
                "code": "key_not_read_only", "permissions": error.problems}) from error
        except Exception as error:  # noqa: BLE001 - wrong key, network, region
            raise HTTPException(status_code=502, detail="key_check_failed") from error
        engine.store.put("account_key", {"key": key, "secret": secret,
                                         "checked_ms": engine.now_ms()})
        engine.log("info", "account_linked", "read-only key " + key[-4:])
        return await account_status()

    @app.delete("/account", dependencies=secured)
    async def unlink_account() -> dict[str, Any]:
        engine.store.put("account_key", None)
        engine.log("info", "account_unlinked")
        return await account_status()

    @app.post("/account/sync", dependencies=secured)
    async def sync_account(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        trader_id = body.get("trader_id") or None
        try:
            return await engine.sync_account(trader_id)
        except account.NotReadOnly as error:
            raise HTTPException(status_code=422, detail={
                "code": "key_not_read_only", "permissions": error.problems}) from error
        except (KeyError, ValueError) as error:
            raise lab_error(error) from error

    @app.get("/sonic.apk", include_in_schema=False)
    async def download_apk() -> FileResponse:
        if not apk.is_file():
            raise HTTPException(status_code=404, detail="apk_not_built")
        return FileResponse(apk, media_type="application/vnd.android.package-archive",
                            filename="sonic.apk")

    web = Path(__file__).resolve().parent.parent / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

    return app
