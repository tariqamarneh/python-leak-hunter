"""Command line: `leak-hunter check` and `leak-hunter why`, aimed at a small harness function.

    leak-hunter check harness:handle_one_request --iterations 30 --trace
    leak-hunter why harness:handle_many_requests myapp.graph.Graph

`check` runs a zero-argument function repeatedly and exits 1 if objects accumulate.
`why` runs a setup function once, then explains what keeps instances of a type alive.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib
import inspect
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .growth import awaited, measure_growth, type_name
from .retention import why_alive


def _load(spec: str) -> Callable[[], Any]:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"expected module:function, got {spec!r}")
    sys.path.insert(0, os.getcwd())
    target: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise SystemExit(f"{spec} is not callable")
    return target  # type: ignore[no-any-return]  # verified callable above


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="leak-hunter", description="Find memory leaks in long-running Python services."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="find object types that accumulate per call")
    check.add_argument("work", help="module:function taking no arguments (sync or async)")
    check.add_argument("--iterations", type=int, default=20)
    check.add_argument("--warmup", type=int, default=3)
    check.add_argument("--min-per-call", type=float, default=0.5)
    check.add_argument("--include", action="append", help="only report types with this name prefix")
    check.add_argument("--ignore", action="append", default=[], help="skip types with this name prefix")
    check.add_argument("--trace", action="store_true", help="also report allocation sites (tracemalloc)")

    why = commands.add_parser("why", help="explain what keeps objects of a type alive")
    why.add_argument("setup", help="module:function to run first, e.g. one that handles 50 requests")
    why.add_argument("type_name", help="type name as printed by `check`, e.g. myapp.Graph")
    why.add_argument("--samples", type=int, default=5)

    args = parser.parse_args(argv)
    if args.command == "check":
        report = measure_growth(
            _load(args.work),
            iterations=args.iterations,
            warmup=args.warmup,
            min_per_call=args.min_per_call,
            include=args.include,
            ignore=args.ignore,
            trace_allocations=args.trace,
        )
        print(report.format())
        return 1 if report.leaked else 0

    setup = _load(args.setup)
    # Only trace instances the setup created, so `why ... list` follows the leaked lists.
    gc.collect()
    existing = {id(obj) for obj in gc.get_objects() if type_name(obj) == args.type_name}
    result = setup()
    if inspect.isawaitable(result):
        asyncio.run(awaited(result))
    del result
    print(why_alive(args.type_name, samples=args.samples, exclude=existing).format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
