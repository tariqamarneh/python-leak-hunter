"""The real thing: a gRPC channel built per request, with the request's context reaching it.

pip install grpcio python-leak-hunter
python examples/grpc_cycle.py
"""

from __future__ import annotations

import contextvars
from typing import Any

import grpc  # type: ignore[import-untyped, unused-ignore]

from leak_hunter import measure_growth, why_alive

current_run: contextvars.ContextVar[RunTrace] = contextvars.ContextVar("current_run")


class RunTrace:
    """Per-request state. Like a run tree whose inputs include the client, it reaches the channel."""

    channel: Any = None


def _auth(context: Any, callback: Any) -> None:
    callback((("authorization", "Bearer token"),), None)


def _channel() -> Any:
    credentials = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(), grpc.metadata_call_credentials(_auth)
    )
    return grpc.secure_channel("localhost:50051", credentials)


def handle_request_leaky() -> None:
    run = RunTrace()
    token = current_run.set(run)
    try:
        run.channel = _channel()  # credentials capture this request's context
    finally:
        current_run.reset(token)


_shared = contextvars.Context().run(_channel)  # once per process, in an empty context


def handle_request_fixed() -> None:
    run = RunTrace()
    token = current_run.set(run)
    try:
        run.channel = _shared
    finally:
        current_run.reset(token)


if __name__ == "__main__":
    print(measure_growth(handle_request_leaky, include=[f"{__name__}."]).format(), end="\n\n")
    print(why_alive(f"{__name__}.RunTrace").format(), end="\n\n")
    print(measure_growth(handle_request_fixed, include=[f"{__name__}."]).format())
