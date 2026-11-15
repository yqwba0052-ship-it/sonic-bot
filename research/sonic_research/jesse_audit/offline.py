"""Run code with every network connection refused, to prove it needs no service."""

from __future__ import annotations

import contextlib
import shutil
import socket
from collections.abc import Iterator


class NetworkBlocked(RuntimeError):
    pass


@contextlib.contextmanager
def block_network() -> Iterator[None]:
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def refuse_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        raise NetworkBlocked(f"network_blocked: {host}")

    def refuse_lookup(host, *args, **kwargs):
        raise NetworkBlocked(f"network_blocked: {host}")

    socket.socket.connect = refuse_connect
    socket.getaddrinfo = refuse_lookup
    try:
        yield
    finally:
        socket.socket.connect = connect
        socket.getaddrinfo = getaddrinfo


def disk_free_bytes(path: str = "C:/") -> int:
    return shutil.disk_usage(path).free
