"""The leak this project grew out of, reproduced with real grpcio (skipped when it is not installed)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from leak_hunter import measure_growth, why_alive

pytest.importorskip("grpc")

spec = importlib.util.spec_from_file_location(
    "grpc_cycle", Path(__file__).parent.parent / "examples/grpc_cycle.py"
)
assert spec is not None and spec.loader is not None
grpc_cycle = importlib.util.module_from_spec(spec)
sys.modules["grpc_cycle"] = grpc_cycle
spec.loader.exec_module(grpc_cycle)


def test_per_request_channel_leaks_and_is_traced_to_the_grpc_plugin() -> None:
    assert measure_growth(grpc_cycle.handle_request_leaky, include=["grpc_cycle."], iterations=10).leaked

    report = why_alive("grpc_cycle.RunTrace")

    assert report.outside_python
    path, _ = report.paths[0]
    assert "grpc._plugin_wrapping._Plugin" in path.root
    assert path.steps[0] == "grpc._plugin_wrapping._Plugin._stored_ctx"


def test_shared_channel_built_in_an_empty_context_does_not_leak() -> None:
    assert not measure_growth(grpc_cycle.handle_request_fixed, include=["grpc_cycle."], iterations=10).leaked
