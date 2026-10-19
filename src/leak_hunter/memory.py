"""Sample process memory over time and tell real retention apart from allocator effects.

RSS alone cannot distinguish "objects are being kept" from "freed memory is not returned to
the OS" (fragmentation) or "a native library is allocating outside malloc". On glibc,
`mallinfo2()` splits malloc's heap into bytes in use and bytes free, which separates them.
"""

from __future__ import annotations

import ctypes
import gc
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from types import TracebackType


@dataclass(frozen=True)
class MemorySample:
    time: float
    rss_bytes: int | None
    heap_in_use_bytes: int | None
    heap_free_bytes: int | None
    gc_objects: int
    open_fds: int | None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class _MallInfo2(ctypes.Structure):
    _fields_ = [
        (field, ctypes.c_size_t)
        for field in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


def _load_mallinfo2() -> Callable[[], _MallInfo2] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        fn = ctypes.CDLL(None).mallinfo2
    except (OSError, AttributeError):  # musl, or glibc older than 2.33
        return None
    fn.restype = _MallInfo2
    fn.argtypes = []
    return fn


_mallinfo2 = _load_mallinfo2()


def _rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore[import-untyped, unused-ignore]  # optional dependency

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        pass
    try:
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        pass
    try:  # macOS and other BSDs without psutil
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
        )
        return int(out.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def _open_fds() -> int | None:
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return None


def sample_memory() -> MemorySample:
    """One reading of this process's memory. Heap fields are None off glibc."""
    heap_in_use = heap_free = None
    if _mallinfo2 is not None:
        info = _mallinfo2()
        heap_in_use = info.uordblks + info.hblkhd
        heap_free = info.fordblks
    return MemorySample(
        time=time.time(),
        rss_bytes=_rss_bytes(),
        heap_in_use_bytes=heap_in_use,
        heap_free_bytes=heap_free,
        gc_objects=len(gc.get_objects()),
        open_fds=_open_fds(),
    )


class MemorySampler:
    """Samples memory on a background thread, e.g. once a minute in a running service.

    Each sample goes to `on_sample` (log it as one JSON line with `sample.to_json()`) and is
    kept in `samples` for `diagnose()`. Use as a context manager or call start()/stop().
    """

    def __init__(
        self,
        interval: float = 60.0,
        on_sample: Callable[[MemorySample], None] | None = None,
        keep: int = 1_440,
    ) -> None:
        self.interval = interval
        self.samples: list[MemorySample] = []
        self._on_sample = on_sample
        self._keep = keep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> MemorySampler:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="leak-hunter-sampler", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def __enter__(self) -> MemorySampler:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        while True:
            sample = sample_memory()
            self.samples.append(sample)
            del self.samples[: -self._keep]
            if self._on_sample is not None:
                self._on_sample(sample)
            if self._stop.wait(self.interval):
                return


def slope_per_hour(samples: Sequence[MemorySample], field: str) -> float | None:
    """Least-squares growth of a numeric sample field, in units per hour."""
    points = [(s.time, getattr(s, field)) for s in samples if getattr(s, field) is not None]
    if len(points) < 3:
        return None
    mean_t = sum(t for t, _ in points) / len(points)
    mean_v = sum(v for _, v in points) / len(points)
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    if spread == 0:
        return None
    return float(sum((t - mean_t) * (v - mean_v) for t, v in points) / spread * 3600)


def diagnose(samples: Sequence[MemorySample], min_mb_per_hour: float = 10.0) -> str:
    """Classify memory growth from samples taken over a meaningful stretch of real traffic."""
    mb = 1024 * 1024
    rss = slope_per_hour(samples, "rss_bytes")
    if rss is None:
        return "Not enough samples: take at least 3, spread over time, with RSS available."
    if rss < min_mb_per_hour * mb:
        return f"RSS is flat ({rss / mb:+.1f} MB/h). No leak visible in this window."

    lines = [f"RSS grows {rss / mb:+.1f} MB/h."]
    in_use = slope_per_hour(samples, "heap_in_use_bytes")
    free = slope_per_hour(samples, "heap_free_bytes")
    objects = slope_per_hour(samples, "gc_objects")
    fds = slope_per_hour(samples, "open_fds")

    if in_use is not None and free is not None:
        if in_use >= 0.5 * rss:
            lines.append(
                f"malloc heap in use grows {in_use / mb:+.1f} MB/h: memory is being retained, not "
                "fragmented. Find what accumulates (measure_growth), then what holds it (why_alive)."
            )
        elif free >= 0.5 * rss:
            lines.append(
                f"malloc free space grows {free / mb:+.1f} MB/h while in-use is flat: fragmentation. "
                "Try MALLOC_ARENA_MAX=2 or jemalloc before hunting objects."
            )
        else:
            lines.append(
                "malloc's heap explains little of it: memory is allocated outside malloc (mmap by a "
                "native library, thread stacks). Profile natively with memray --native or jemalloc."
            )
    elif objects is not None and objects > 0:
        lines.append(
            f"The live Python object count grows {objects:+.0f}/h: objects are accumulating. "
            "Run measure_growth on one unit of work."
        )
    else:
        lines.append(
            "The Python object count is flat, so the growth is native memory or fragmentation. "
            "No mallinfo2 here (not glibc): use memray --native, or test under Linux."
        )
    if fds is not None and fds > 1:
        lines.append(
            f"Open file descriptors grow {fds:+.0f}/h: connections or files are not being closed "
            "(clients created per request?)."
        )
    return "\n".join(lines)
