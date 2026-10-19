from __future__ import annotations

import ctypes
from collections.abc import Callable, Generator

import pytest

from leak_hunter import why_alive


class Thing:
    pass


class NativelyHeld:
    """Leaked on purpose by one test, so it gets a type of its own."""


class Registry:
    def __init__(self) -> None:
        self.items: list[Thing] = []


_held: list[Thing] = []
registry = Registry()


@pytest.fixture(autouse=True)
def _reset() -> None:
    _held.clear()
    registry.items.clear()


def test_traces_to_a_module_global() -> None:
    _held.extend(Thing() for _ in range(3))

    report = why_alive(f"{__name__}.Thing")

    assert report.instances == 3
    [(path, count)] = report.paths
    assert count == 3
    assert path.root == f"module {__name__}, global '_held'"
    assert path.steps[0].startswith("list[")
    assert path.steps[-1] == f"{__name__}.Thing  <- the object being explained"
    assert not path.outside_python


def test_names_attributes_along_the_way() -> None:
    registry.items.append(Thing())

    [(path, _)] = why_alive(Thing).paths

    assert path.root == f"module {__name__}, global 'registry'"
    assert path.steps[0] == f"{__name__}.Registry.items"


def test_accepts_a_single_object_and_ignores_the_callers_own_variable() -> None:
    thing = Thing()
    _held.append(thing)

    report = why_alive(thing)

    [(path, _)] = report.paths
    assert path.root == f"module {__name__}, global '_held'"


def test_reports_objects_held_where_the_garbage_collector_cannot_see() -> None:
    holder = {"thing": NativelyHeld()}
    ctypes.pythonapi.Py_IncRef(ctypes.py_object(holder))  # what a C extension does
    del holder

    report = why_alive(NativelyHeld)

    assert report.outside_python
    [(path, _)] = report.paths
    assert path.root.startswith("outside Python: nothing the garbage collector can see refers to this dict")
    assert path.steps == ("dict['thing']", f"{__name__}.NativelyHeld  <- the object being explained")


def test_follows_closures() -> None:
    def make() -> Callable[[], Thing]:
        captured = Thing()
        return lambda: captured

    callback = make()

    [(path, _)] = why_alive(Thing).paths

    assert path.root.startswith("variable callback in test_follows_closures()")
    assert "cell (closure variable)" in path.steps
    assert callback() is not None


def test_follows_suspended_generators() -> None:
    def worker() -> Generator[None, None, None]:
        local = Thing()
        yield
        assert local

    running = worker()
    next(running)

    [(path, _)] = why_alive(Thing).paths

    # Python 3.10 exposes the suspended frame itself; 3.11+ only the generator object.
    assert path.root.startswith(
        ("variable local in worker()", "variable running in test_follows_suspended_generators()")
    )


def test_groups_instances_that_share_a_chain() -> None:
    _held.extend(Thing() for _ in range(20))
    registry.items.append(Thing())

    report = why_alive(Thing, samples=21)

    assert report.instances == 21
    assert [count for _, count in report.paths] == [20, 1]
    assert "20 of 21 held by:" in report.format()


def test_no_instances() -> None:
    report = why_alive("nowhere.Missing")

    assert report.instances == 0
    assert report.format() == "No live nowhere.Missing objects found."
