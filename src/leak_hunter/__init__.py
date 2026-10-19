"""Find memory leaks in long-running Python services.

- `measure_growth` / `assert_no_leak`: which object types accumulate per unit of work.
- `why_alive`: what keeps them alive, down to "held by native code".
- `MemorySampler` / `diagnose`: is it really a leak, or fragmentation, or native memory.
"""

from .growth import (
    GrowthReport,
    LeakError,
    TypeGrowth,
    assert_no_leak,
    assert_no_leak_async,
    count_objects,
    measure_growth,
    measure_growth_async,
)
from .memory import MemorySample, MemorySampler, diagnose, sample_memory
from .retention import RetentionPath, RetentionReport, why_alive

__all__ = [
    "GrowthReport",
    "LeakError",
    "MemorySample",
    "MemorySampler",
    "RetentionPath",
    "RetentionReport",
    "TypeGrowth",
    "assert_no_leak",
    "assert_no_leak_async",
    "count_objects",
    "diagnose",
    "measure_growth",
    "measure_growth_async",
    "sample_memory",
    "why_alive",
]

__version__ = "0.1.0"
