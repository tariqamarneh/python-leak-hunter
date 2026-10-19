"""A request handler that leaks the way a gRPC client built per request does.

gRPC's Python auth plugin stores `contextvars.copy_context()` when call credentials are
created, and the native channel keeps that plugin. Build a client inside a request and the
request's context, with everything in its context variables (tracing run trees, callbacks,
the agent graph), is now reachable from C code. If anything in that context reaches the
channel back, the cycle passes through C, the garbage collector cannot see it, and none of
it is ever freed. examples/grpc_cycle.py shows the same leak with real grpcio.

`_hold_in_native_code` stands in for the C extension so this demo needs no gRPC install.
"""

from __future__ import annotations

import contextvars
import ctypes
import threading
from typing import Any

current_run: contextvars.ContextVar[RunTrace] = contextvars.ContextVar("current_run")


class RunTrace:
    """Per-request tracing state, like a LangSmith run tree. Big on purpose."""

    def __init__(self) -> None:
        self.payload = bytearray(200_000)
        self.children: list[Any] = []


def _hold_in_native_code(obj: object) -> None:
    """Take a reference the way a C extension does, invisible to the garbage collector."""
    ctypes.pythonapi.Py_IncRef(ctypes.py_object(obj))


class SearchClient:
    """A client whose constructor captures the caller's context, like grpc credentials."""

    def __init__(self) -> None:
        _hold_in_native_code(contextvars.copy_context())

    def search(self, query: str) -> list[str]:
        return [query.upper()]


def handle_request_leaky(query: str = "shoes") -> list[str]:
    token = current_run.set(RunTrace())
    try:
        client = SearchClient()  # built inside the request: its context is captured
        return client.search(query)
    finally:
        current_run.reset(token)


_client: SearchClient | None = None
_client_lock = threading.Lock()


def get_client() -> SearchClient:
    """One client per process, constructed in an empty context so it captures nothing."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = contextvars.Context().run(SearchClient)
    return _client


def handle_request_fixed(query: str = "shoes") -> list[str]:
    token = current_run.set(RunTrace())
    try:
        return get_client().search(query)
    finally:
        current_run.reset(token)


def handle_many_leaky() -> None:
    for _ in range(50):
        handle_request_leaky()
