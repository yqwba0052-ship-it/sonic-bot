"""Run the server: python -m sonic"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import httpx
import uvicorn

from .api import create_app
from .config import load_settings
from .engine import Engine
from .exchange import DEMO_URL, PUBLIC_URL, BinanceDemo, PaperExchange, PublicMarket
from .store import Store


def lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])


def main() -> None:
    settings = load_settings(Path(__file__).resolve().parent.parent)
    store = Store(settings.db_path)
    if settings.mode == "demo":
        client = httpx.AsyncClient(base_url=DEMO_URL, timeout=10)
        exchange = BinanceDemo(client, settings.demo_key, settings.demo_secret)
    else:
        client = httpx.AsyncClient(base_url=PUBLIC_URL, timeout=10)
        exchange = PaperExchange(PublicMarket(client), store, settings.paper_balance)
    engine = Engine(store, exchange, settings)
    print(f"Mode: {settings.mode}")
    public_url = os.environ.get("SONIC_PUBLIC_URL", "")
    address = public_url or f"http://{lan_address()}:{settings.port}"
    print(f"Server address for the app: {address}")
    print(f"Token for the app: {settings.token}")
    uvicorn.run(create_app(engine, settings.token), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
