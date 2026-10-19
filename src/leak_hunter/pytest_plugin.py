"""pytest plugin: the `leak_check` fixture, registered automatically when the package is installed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from .growth import GrowthReport, Work, assert_no_leak


@pytest.fixture
def leak_check() -> Callable[..., GrowthReport]:
    """`leak_check(work, **options)` fails the test if `work` leaks. Same options as `measure_growth`."""

    def check(work: Work, **options: Any) -> GrowthReport:
        return assert_no_leak(work, **options)

    return check
