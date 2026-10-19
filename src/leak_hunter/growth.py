"""Find object types that keep accumulating when the same unit of work runs repeatedly.

The measurement runs the work in two equal windows after a warm-up. A type only counts as
leaking when its live count grows in *both* windows: one-off growth such as a cache filling
up or a lazy import shows up in one window, not both.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import os
import tracemalloc
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Work = Callable[[], Any]
AsyncWork = Callable[[], Awaitable[Any]]

_PACKAGE_DIR = str(Path(__file__).parent)


def type_name(obj: object) -> str:
    """Fully qualified type name, without the noise of `builtins.`."""
    cls = type(obj)
    module = cls.__module__
    return cls.__qualname__ if module == "builtins" else f"{module}.{cls.__qualname__}"


def count_objects() -> Counter[str]:
    """Count live objects tracked by the garbage collector, by type name.

    Only container-like objects are tracked (class instances, dicts, lists, closures...).
    Plain strings, ints and bytes are not, so they never appear here; `trace_allocations`
    covers them by allocation site instead.
    """
    gc.collect()
    counts: Counter[str] = Counter()
    for obj in gc.get_objects():
        counts[type_name(obj)] += 1
    return counts


@dataclass(frozen=True)
class TypeGrowth:
    type_name: str
    first_window: int
    second_window: int
    iterations: int

    @property
    def per_call(self) -> float:
        return self.second_window / self.iterations


@dataclass(frozen=True)
class AllocationGrowth:
    location: str
    size_bytes: int
    blocks: int


@dataclass(frozen=True)
class GrowthReport:
    iterations: int
    growing: tuple[TypeGrowth, ...]
    allocations: tuple[AllocationGrowth, ...] = ()

    @property
    def leaked(self) -> bool:
        return bool(self.growing)

    @property
    def suspect(self) -> str | None:
        """The type to trace next: the first one defined in a module, not a builtin like `list`."""
        for growth in self.growing:
            if "." in growth.type_name:
                return growth.type_name
        return self.growing[0].type_name if self.growing else None

    def format(self, limit: int = 10) -> str:
        lines = []
        if not self.leaked:
            lines.append(f"No object type grew steadily over 2 x {self.iterations} calls.")
        else:
            shown = self.growing[:limit]
            width = max(len(g.type_name) for g in shown)
            lines.append(f"Objects accumulating across calls (2 windows x {self.iterations} calls):")
            for g in shown:
                lines.append(
                    f"  {g.type_name:<{width}}  +{_number(g.per_call)} per call"
                    f"  (window 1: {g.first_window:+d}, window 2: {g.second_window:+d})"
                )
            if len(self.growing) > limit:
                lines.append(f"  ... and {len(self.growing) - limit} more types")
        if self.allocations:
            # Covers what the object count cannot: bytes, str and other untracked objects.
            lines.append(f"Allocation sites still holding memory from the last {self.iterations} calls:")
            for a in self.allocations:
                per_call = a.size_bytes / self.iterations / 1024
                lines.append(f"  {a.location}  +{_number(per_call)} KiB per call  ({a.blocks:+d} blocks)")
        if self.suspect is not None:
            lines.append("Next, see what keeps them alive:")
            lines.append(f"  leak-hunter why <module:function that runs many calls> {self.suspect}")
            lines.append(f"  or in Python: why_alive({self.suspect!r})")
        return "\n".join(lines)


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _short_path(path: str) -> str:
    """Relative to the working directory when inside it, so report lines stay readable."""
    relative = os.path.relpath(path)
    return path if relative.startswith("..") else relative


class LeakError(AssertionError):
    """Raised by `assert_no_leak` when objects keep accumulating."""

    def __init__(self, report: GrowthReport) -> None:
        super().__init__(report.format())
        self.report = report


class _Windows:
    """Takes the three object counts around the two measured windows."""

    def __init__(
        self,
        *,
        iterations: int,
        min_per_call: float,
        include: Iterable[str] | None,
        ignore: Iterable[str],
        trace_allocations: bool,
    ) -> None:
        if iterations < 1:
            raise ValueError("iterations must be at least 1")
        self._iterations = iterations
        self._threshold = min_per_call * iterations
        self._include = tuple(include) if include is not None else None
        self._ignore = tuple(ignore)
        self._trace = trace_allocations
        self._started_tracing = False
        self._before: Counter[str] = Counter()
        self._middle: Counter[str] = Counter()
        self._middle_snapshot: tracemalloc.Snapshot | None = None

    def start(self) -> None:
        if self._trace and not tracemalloc.is_tracing():
            tracemalloc.start()
            self._started_tracing = True
        self._before = count_objects()

    def middle(self) -> None:
        self._middle = count_objects()
        if self._trace:
            self._middle_snapshot = tracemalloc.take_snapshot()

    def finish(self) -> GrowthReport:
        after = count_objects()
        allocations = self._allocations() if self._trace else ()
        growing = []
        for name, count in after.items():
            first = self._middle[name] - self._before[name]
            second = count - self._middle[name]
            if first >= self._threshold and second >= self._threshold and self._wanted(name):
                growing.append(TypeGrowth(name, first, second, self._iterations))
        growing.sort(key=lambda g: g.second_window, reverse=True)
        return GrowthReport(self._iterations, tuple(growing), allocations)

    def close(self) -> None:
        if self._started_tracing:
            tracemalloc.stop()
            self._started_tracing = False

    def _wanted(self, name: str) -> bool:
        if any(name.startswith(prefix) for prefix in self._ignore):
            return False
        return self._include is None or any(name.startswith(prefix) for prefix in self._include)

    def _allocations(self, limit: int = 5) -> tuple[AllocationGrowth, ...]:
        if self._middle_snapshot is None:
            return ()
        exclude = [
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, f"{_PACKAGE_DIR}/*"),
        ]
        after = tracemalloc.take_snapshot().filter_traces(exclude)
        before = self._middle_snapshot.filter_traces(exclude)
        found = []
        for stat in after.compare_to(before, "lineno"):
            if stat.size_diff > 0 and stat.count_diff > 0:
                frame = stat.traceback[0]
                location = f"{_short_path(frame.filename)}:{frame.lineno}"
                found.append(AllocationGrowth(location, stat.size_diff, stat.count_diff))
            if len(found) == limit:
                break
        return tuple(found)


def measure_growth(
    work: Work,
    *,
    iterations: int = 20,
    warmup: int = 3,
    min_per_call: float = 0.5,
    include: Iterable[str] | None = None,
    ignore: Iterable[str] = (),
    trace_allocations: bool = False,
) -> GrowthReport:
    """Run `work` repeatedly and report object types that keep accumulating.

    `work` takes no arguments and may be a plain function or return an awaitable (an
    `async def` works); awaitables run on a private event loop. Inside an already running
    loop, use `measure_growth_async` instead.

    A type is reported when it gains at least `min_per_call` objects per call in both
    windows. `include` / `ignore` filter by type-name prefix, e.g. `include=["myapp."]`.
    """
    windows = _Windows(
        iterations=iterations,
        min_per_call=min_per_call,
        include=include,
        ignore=ignore,
        trace_allocations=trace_allocations,
    )
    loop = asyncio.new_event_loop()

    def run(times: int) -> None:
        for _ in range(times):
            result = work()
            if inspect.isawaitable(result):
                loop.run_until_complete(awaited(result))

    try:
        run(warmup)
        windows.start()
        run(iterations)
        windows.middle()
        run(iterations)
        return windows.finish()
    finally:
        windows.close()
        loop.close()


async def measure_growth_async(
    work: AsyncWork,
    *,
    iterations: int = 20,
    warmup: int = 3,
    min_per_call: float = 0.5,
    include: Iterable[str] | None = None,
    ignore: Iterable[str] = (),
    trace_allocations: bool = False,
) -> GrowthReport:
    """`measure_growth` for code that already runs inside an event loop."""
    windows = _Windows(
        iterations=iterations,
        min_per_call=min_per_call,
        include=include,
        ignore=ignore,
        trace_allocations=trace_allocations,
    )

    async def run(times: int) -> None:
        for _ in range(times):
            await work()

    try:
        await run(warmup)
        windows.start()
        await run(iterations)
        windows.middle()
        await run(iterations)
        return windows.finish()
    finally:
        windows.close()


def assert_no_leak(work: Work, **options: Any) -> GrowthReport:
    """Fail with a readable report if `work` leaks objects. Takes `measure_growth`'s options."""
    report = measure_growth(work, **options)
    if report.leaked:
        raise LeakError(report)
    return report


async def assert_no_leak_async(work: AsyncWork, **options: Any) -> GrowthReport:
    """`assert_no_leak` for code that already runs inside an event loop."""
    report = await measure_growth_async(work, **options)
    if report.leaked:
        raise LeakError(report)
    return report


async def awaited(awaitable: Awaitable[Any]) -> Any:
    return await awaitable
