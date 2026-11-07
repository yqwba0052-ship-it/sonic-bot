"""HTTP API for the phone app. Every route except /health needs the bearer token."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .risk import Limits


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
    async def put_limits(limits: Limits) -> dict[str, Any]:
        try:
            await engine.set_limits(limits)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return limits.model_dump()

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

    web = Path(__file__).resolve().parent.parent / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

    return app
