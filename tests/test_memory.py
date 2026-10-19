from __future__ import annotations

import json
import time

from leak_hunter import MemorySample, MemorySampler, diagnose, sample_memory

MB = 1024 * 1024


def series(
    hours: float = 2.0,
    rss: float = 0.0,
    in_use: float | None = 0.0,
    free: float | None = 0.0,
    objects: float = 0.0,
    fds: float = 0.0,
) -> list[MemorySample]:
    """Synthetic samples growing linearly at the given rates (MB/h, or count/h)."""
    samples = []
    for i in range(13):
        h = hours * i / 12
        samples.append(
            MemorySample(
                time=h * 3600,
                rss_bytes=int(500 * MB + rss * MB * h),
                heap_in_use_bytes=None if in_use is None else int(200 * MB + in_use * MB * h),
                heap_free_bytes=None if free is None else int(20 * MB + free * MB * h),
                gc_objects=int(100_000 + objects * h),
                open_fds=int(50 + fds * h),
            )
        )
    return samples


def test_sample_memory_reads_this_process() -> None:
    sample = sample_memory()

    assert sample.rss_bytes is not None and sample.rss_bytes > 10 * MB
    assert sample.gc_objects > 1_000
    assert json.loads(sample.to_json())["rss_bytes"] == sample.rss_bytes


def test_sampler_collects_on_a_background_thread() -> None:
    seen: list[MemorySample] = []
    with MemorySampler(interval=0.01, on_sample=seen.append, keep=3) as sampler:
        deadline = time.time() + 5
        while len(seen) < 5 and time.time() < deadline:
            time.sleep(0.01)

    assert len(seen) >= 5
    assert len(sampler.samples) == 3  # only the most recent `keep` are retained
    count = len(seen)
    time.sleep(0.05)
    assert len(seen) == count  # stopped


def test_not_enough_samples() -> None:
    assert diagnose(series()[:2]).startswith("Not enough samples")


def test_flat_rss() -> None:
    assert diagnose(series(rss=1)).startswith("RSS is flat (+1.0 MB/h)")


def test_heap_in_use_growing_means_real_retention() -> None:
    result = diagnose(series(rss=300, in_use=290))

    assert "RSS grows +300.0 MB/h" in result
    assert "memory is being retained, not fragmented" in result


def test_free_space_growing_means_fragmentation() -> None:
    assert "fragmentation" in diagnose(series(rss=300, in_use=5, free=280))


def test_growth_outside_malloc_points_to_native_profiling() -> None:
    assert "allocated outside malloc" in diagnose(series(rss=300, in_use=5, free=5))


def test_without_mallinfo_falls_back_to_the_object_count() -> None:
    assert "objects are accumulating" in diagnose(series(rss=300, in_use=None, free=None, objects=50_000))
    assert "native memory or fragmentation" in diagnose(series(rss=300, in_use=None, free=None))


def test_growing_file_descriptors_are_called_out() -> None:
    assert "file descriptors grow +40/h" in diagnose(series(rss=300, in_use=290, fds=40))
