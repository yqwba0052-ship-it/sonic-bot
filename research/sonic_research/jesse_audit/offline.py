"""Run code with every network connection refused, to prove it needs no service."""

from __future__ import annotations

import contextlib
import shutil
import socket
from collections.abc import Iterator

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class NetworkBlocked(RuntimeError):
    pass


@contextlib.contextmanager
def block_network(allow_loopback: bool = False) -> Iterator[None]:
    """Refuse every connection; with allow_loopback, only this computer (127.0.0.1) is allowed,
    which Ray needs to talk to its own local worker processes."""
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo
    connect_ex, sendto = socket.socket.connect_ex, socket.socket.sendto

    def local(host) -> bool:
        return allow_loopback and host in LOOPBACK

    def refuse_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if local(host):
            return connect(self, address, *args, **kwargs)
        raise NetworkBlocked(f"network_blocked: {host}")

    def refuse_lookup(host, *args, **kwargs):
        if local(host):
            return getaddrinfo(host, *args, **kwargs)
        raise NetworkBlocked(f"network_blocked: {host}")

    def refuse_connect_ex(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if local(host):
            return connect_ex(self, address, *args, **kwargs)
        raise NetworkBlocked(f"network_blocked: {host}")

    def refuse_sendto(self, data, *args):
        address = args[-1]
        host = address[0] if isinstance(address, tuple) else address
        if local(host):
            return sendto(self, data, *args)
        raise NetworkBlocked(f"network_blocked: {host}")

    socket.socket.connect = refuse_connect
    socket.socket.connect_ex = refuse_connect_ex
    socket.socket.sendto = refuse_sendto
    socket.getaddrinfo = refuse_lookup
    try:
        yield
    finally:
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.socket.sendto = sendto
        socket.getaddrinfo = getaddrinfo


def disk_free_bytes(path: str = "C:/") -> int:
    return shutil.disk_usage(path).free
