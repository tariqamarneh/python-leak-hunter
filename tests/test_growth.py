from __future__ import annotations

import asyncio
import os

import pytest

from leak_hunter import LeakError, assert_no_leak, assert_no_leak_async, measure_growth, measure_growth_async


class Session:
    pass


class Unrelated:
    pass


_sessions: list[Session] = []
_cache: dict[int, Session] = {}
_blobs: list[bytes] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    _sessions.clear()
    _cache.clear()
    _blobs.clear()


def leaky() -> None:
    _sessions.append(Session())


def clean() -> None:
    Session()


def fills_a_cache() -> None:
    # Grows until 15 entries, then stops: growth in the first window only.
    if len(_cache) < 15:
        _cache[len(_cache)] = Session()


def test_reports_a_type_that_grows_every_call() -> None:
    report = measure_growth(leaky, iterations=10)

    assert report.leaked
    [growth] = [g for g in report.growing if g.type_name.endswith("Session")]
    assert growth.type_name == f"{__name__}.Session"
    assert (growth.first_window, growth.second_window, growth.per_call) == (10, 10, 1)


def test_clean_work_is_not_reported() -> None:
    assert not measure_growth(clean, iterations=10).leaked


def test_one_off_growth_like_a_cache_filling_is_not_a_leak() -> None:
    assert not measure_growth(fills_a_cache, warmup=3, iterations=20).leaked


def test_include_and_ignore_filter_by_type_name_prefix() -> None:
    assert measure_growth(leaky, iterations=5, include=[__name__]).leaked
    assert not measure_growth(leaky, iterations=5, include=["somewhere.else"]).leaked
    assert not measure_growth(leaky, iterations=5, ignore=[f"{__name__}.Session"]).leaked


def test_async_work_runs_on_a_private_loop() -> None:
    async def handler() -> None:
        await asyncio.sleep(0)
        _sessions.append(Session())

    assert measure_growth(handler, iterations=5).leaked


def test_async_variant_runs_inside_an_existing_loop() -> None:
    async def handler() -> None:
        _sessions.append(Session())

    async def scenario() -> None:
        report = await measure_growth_async(handler, iterations=5)
        assert report.leaked
        with pytest.raises(LeakError):
            await assert_no_leak_async(handler, iterations=5)
        _sessions.clear()
        await assert_no_leak_async(lambda: asyncio.sleep(0), iterations=5)

    asyncio.run(scenario())


def test_assert_no_leak_raises_a_readable_report() -> None:
    with pytest.raises(LeakError) as caught:
        assert_no_leak(leaky, iterations=5)

    message = str(caught.value)
    assert f"{__name__}.Session  +1 per call" in message
    assert f"why_alive('{__name__}.Session')" in message
    assert caught.value.report.leaked


def test_assert_no_leak_returns_the_report_when_clean() -> None:
    assert not assert_no_leak(clean, iterations=5).leaked


def test_allocation_tracing_catches_leaks_the_object_count_cannot_see() -> None:
    def keeps_bytes() -> None:
        _blobs.append(bytes(4096))  # bytes are not tracked by the garbage collector

    report = measure_growth(keeps_bytes, iterations=10, trace_allocations=True)

    assert not report.leaked
    assert report.allocations
    assert report.allocations[0].location.startswith(os.path.relpath(__file__))
    assert report.allocations[0].size_bytes >= 10 * 4096
    assert "KiB per call" in report.format()


def test_next_step_names_the_projects_own_type_not_a_builtin() -> None:
    kept: list[list[Session]] = []

    def leaks_two_types() -> None:
        kept.append([Session()])  # a list and a Session per call

    report = measure_growth(leaks_two_types, iterations=10)
    names = {g.type_name for g in report.growing}

    assert {"list", f"{__name__}.Session"} <= names
    assert report.suspect == f"{__name__}.Session"
    assert f"leak-hunter why <module:function that runs many calls> {__name__}.Session" in report.format()


def test_per_call_numbers_are_rounded() -> None:
    calls = 0

    def three_every_other_call() -> None:
        nonlocal calls
        calls += 1
        if calls % 2:
            _sessions.extend(Session() for _ in range(3))

    report = measure_growth(three_every_other_call, iterations=7, warmup=0)

    # Odd calls only: window 2 (calls 8-14) adds 3 x 3 = 9 objects over 7 calls.
    assert f"{__name__}.Session  +1.29 per call" in report.format()


def test_rejects_zero_iterations() -> None:
    with pytest.raises(ValueError):
        measure_growth(clean, iterations=0)
