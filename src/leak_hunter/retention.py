"""Explain what keeps an object alive by walking its referrers back to a root.

A root is a module global or a variable in a running frame. When the walk instead reaches an
object that nothing in Python refers to, yet it is still alive, the reference comes from
somewhere the garbage collector cannot see: usually a C extension (gRPC, a database driver,
an HTTP client) that stored the object without exposing it.
"""

from __future__ import annotations

import gc
import re
import sys
import types
from collections import Counter, deque
from collections.abc import Collection
from dataclasses import dataclass

from .growth import type_name

_THIS_FILE = __file__


@dataclass(frozen=True)
class RetentionPath:
    """One chain of references, root first and the object being explained last."""

    steps: tuple[str, ...]
    root: str
    outside_python: bool

    def format(self) -> str:
        lines = [f"  {self.root}"]
        lines += [f"    -> {step}" for step in self.steps]
        return "\n".join(lines)


@dataclass(frozen=True)
class RetentionReport:
    type_name: str
    instances: int
    paths: tuple[tuple[RetentionPath, int], ...]

    @property
    def outside_python(self) -> bool:
        return any(path.outside_python for path, _ in self.paths)

    def format(self) -> str:
        if not self.paths:
            return f"No live {self.type_name} objects found."
        sampled = sum(count for _, count in self.paths)
        lines = [f"{self.instances} live {self.type_name}; traced {sampled}:"]
        for path, count in self.paths:
            lines.append(f"{count} of {sampled} held by:")
            lines.append(path.format())
        return "\n".join(lines)


def why_alive(
    target: object,
    *,
    samples: int = 5,
    max_nodes: int = 5_000,
    exclude: Collection[int] = (),
) -> RetentionReport:
    """Explain what keeps objects alive.

    `target` is a type, a type name as printed by `measure_growth` (e.g. "myapp.Graph"), or
    a single object. For a type, up to `samples` instances spread across the live ones are
    traced, and identical chains are grouped, so one report covers the common case and the
    exceptions. Only objects tracked by the garbage collector can be found by type.

    `exclude` is a set of object ids to skip, e.g. the instances that existed before the
    code under test ran. That matters for builtin types: tracing "list" otherwise samples
    arbitrary lists from all over the process.
    """
    gc.collect()  # unreachable cycles are garbage, not leaks: free them before looking
    caller = sys._getframe(1)
    skip_frames = {id(caller)} if not isinstance(target, (type, str)) else set()

    if isinstance(target, (type, str)):
        name = target if isinstance(target, str) else _qualified(target)
        picked, total = _pick_instances(name, samples, exclude)
    else:
        name = type_name(target)
        picked = [target]
        total = 1

    module_dicts = {id(vars(m)): m_name for m_name, m in list(sys.modules.items()) if m is not None}
    frame_locals = _running_frame_locals(skip_frames)
    del caller
    # Group by the shape of the chain, so list[3] and list[7] count as the same cause.
    first_of_shape: dict[tuple[str, tuple[str, ...]], RetentionPath] = {}
    counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
    for obj in picked:
        path = _trace(obj, {id(picked)}, skip_frames, module_dicts, frame_locals, max_nodes)
        shape = (path.root, tuple(_INDEX.sub("[..]", step) for step in path.steps))
        first_of_shape.setdefault(shape, path)
        counts[shape] += 1
    del picked
    return RetentionReport(name, total, tuple((first_of_shape[s], n) for s, n in counts.most_common()))


_INDEX = re.compile(r"\[[^\]]*\]")


def _running_frame_locals(skip_frames: set[int]) -> dict[int, str]:
    """Map id(value) to a description, for locals of functions still executing on any thread.

    Since Python 3.11 the garbage collector does not report executing frames as referrers, so
    without this an object held only by a running function's variable would look native.
    """
    found: dict[int, str] = {}
    for top in sys._current_frames().values():
        frame: types.FrameType | None = top
        while frame is not None:
            code = frame.f_code
            if id(frame) not in skip_frames and code.co_filename != _THIS_FILE:
                where = f"in {code.co_name}() at {code.co_filename}:{frame.f_lineno}"
                for var, value in frame.f_locals.items():
                    found.setdefault(id(value), f"variable {var} {where}")
            frame = frame.f_back
    return found


def _qualified(cls: type) -> str:
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _pick_instances(name: str, samples: int, exclude: Collection[int]) -> tuple[list[object], int]:
    """Up to `samples` evenly spaced instances of the named type, and how many are alive."""
    found = [obj for obj in gc.get_objects() if type_name(obj) == name and id(obj) not in exclude]
    total = len(found)
    if total <= samples:
        picked: list[object] = list(found)
    else:
        step = (total - 1) / (samples - 1) if samples > 1 else 0
        picked = [found[round(i * step)] for i in range(samples)]
    del found
    return picked, total


def _trace(
    target: object,
    ignored: set[int],
    skip_frames: set[int],
    module_dicts: dict[int, str],
    frame_locals: dict[int, str],
    max_nodes: int,
) -> RetentionPath:
    """Breadth-first search from `target` up its referrers to the nearest root."""
    nodes: dict[int, object] = {id(target): target}
    # link[holder] = (held, how the holder refers to it), pointing back towards the target.
    link: dict[int, tuple[int, str]] = {}
    queue: deque[int] = deque([id(target)])
    ours = {id(nodes), id(link), id(queue), *ignored}
    dead_end: int | None = None

    while queue and len(nodes) < max_nodes:
        node_id = queue.popleft()
        if node_id in frame_locals:
            return _path(frame_locals[node_id], node_id, nodes, link, outside_python=False)
        holders = [r for r in gc.get_referrers(nodes[node_id]) if not _is_ours(r, ours, skip_frames)]
        ours.add(id(holders))
        try:
            if not holders and dead_end is None:
                dead_end = node_id
            for holder in holders:
                holder_id = id(holder)
                if holder_id in nodes:
                    continue
                how = _how_held(holder, nodes[node_id])
                if holder_id in module_dicts:
                    root = f"module {module_dicts[holder_id]}, global {how.strip('[]')}"
                    return _path(root, node_id, nodes, link, outside_python=False)
                if isinstance(holder, types.FrameType):
                    code = holder.f_code
                    root = (
                        f"variable {how or '?'} in {code.co_name}() at {code.co_filename}:{holder.f_lineno}"
                    )
                    return _path(root, node_id, nodes, link, outside_python=False)
                nodes[holder_id] = holder
                link[holder_id] = (node_id, how)
                queue.append(holder_id)
        finally:
            ours.discard(id(holders))
            del holders

    if dead_end is not None:
        kind = type_name(nodes[dead_end])
        root = (
            f"outside Python: nothing the garbage collector can see refers to this {kind}, "
            "yet it is alive (typically a C extension holding it)"
        )
        return _path(root, dead_end, nodes, link, outside_python=True)
    return RetentionPath((type_name(target),), f"no root found within {max_nodes} objects", False)


def _path(
    root: str,
    start: int,
    nodes: dict[int, object],
    link: dict[int, tuple[int, str]],
    *,
    outside_python: bool,
) -> RetentionPath:
    """Render the chain from `start` down to the target, merging `obj.__dict__['x']` into `obj.x`."""
    hops: list[tuple[str, str]] = []
    current = start
    while True:
        obj = nodes[current]
        nxt = link.get(current)
        hops.append((_label(obj), nxt[1] if nxt else ""))
        if nxt is None:
            break
        current = nxt[0]

    steps: list[str] = []
    i = 0
    while i < len(hops):
        label, how = hops[i]
        if how == ".__dict__" and i + 1 < len(hops) and hops[i + 1][1].startswith("["):
            key = hops[i + 1][1][1:-1].strip("'\"")
            steps.append(f"{label}.{key}")
            i += 2
            continue
        steps.append(f"{label}{how}")
        i += 1
    steps[-1] += "  <- the object being explained"
    return RetentionPath(tuple(steps), root, outside_python)


def _is_ours(obj: object, ours: set[int], skip_frames: set[int]) -> bool:
    if id(obj) in ours or id(obj) in skip_frames:
        return True
    return isinstance(obj, types.FrameType) and obj.f_code.co_filename == _THIS_FILE


def _label(obj: object) -> str:
    if isinstance(obj, type):
        return f"class {obj.__qualname__}"
    if isinstance(obj, types.MethodType):
        return f"bound method {obj.__qualname__}"
    if isinstance(obj, types.FunctionType):
        return f"function {obj.__qualname__}"
    return type_name(obj)


def _how_held(holder: object, held: object) -> str:
    """How `holder` refers to `held`, e.g. "['key']", "[3]" or ".__dict__"."""
    try:
        if isinstance(holder, dict):
            for key, value in holder.items():
                if value is held:
                    return f"[{key!r}]"
                if key is held:
                    return " (dict key)"
            return ""
        if isinstance(holder, (list, tuple)):
            for index, value in enumerate(holder):
                if value is held:
                    return f"[{index}]"
            return ""
        if isinstance(holder, (set, frozenset)):
            return " (set member)"
        if isinstance(holder, types.FrameType):
            for key, value in holder.f_locals.items():
                if value is held:
                    return key
            return ""
        if isinstance(holder, types.MethodType):
            return ".__self__" if holder.__self__ is held else ".__func__"
        if isinstance(holder, types.CellType):
            return " (closure variable)"
        if isinstance(held, dict) and (isinstance(holder, type) or getattr(holder, "__dict__", None) is held):
            return ".__dict__"
        attributes = getattr(holder, "__dict__", None)
        if isinstance(attributes, dict):
            for key, value in attributes.items():
                if value is held:
                    return f".{key}"
    except Exception:  # an exotic holder (proxy, broken __getattr__) just gets no label
        return ""
    return ""
