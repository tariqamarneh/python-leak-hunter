# python-leak-hunter

[![CI](https://github.com/tariqamarneh/python-leak-hunter/actions/workflows/ci.yml/badge.svg)](https://github.com/tariqamarneh/python-leak-hunter/actions/workflows/ci.yml)

Find memory leaks in long-running Python services: **what accumulates**, **what keeps it
alive** (down to "held by a C extension"), and **a test that keeps it fixed**. It ships with an
[agent skill](skills/python-leak-hunter/SKILL.md) that teaches Claude Code, Codex and other
coding agents the whole investigation, not just the tools.

```text
$ leak-hunter why harness:many_requests app.RunTrace

43 live app.RunTrace; traced 5:
5 of 5 held by:
  outside Python: nothing the garbage collector can see refers to this grpc._plugin_wrapping._Plugin, yet it is alive (typically a C extension holding it)
    -> grpc._plugin_wrapping._Plugin._stored_ctx
    -> _contextvars.Context
    -> hamt
    -> hamt_bitmap_node
    -> app.RunTrace  <- the object being explained
```

That output is from [a real grpcio reproduction](examples/grpc_cycle.py): a gRPC channel built
inside a request captures the request's `contextvars` context. When that context can reach
the channel back, the cycle runs through C code and is never freed. In production this pinned
whole AI-agent graphs in memory, about 2 GB every 6 hours per worker.
[The full story](https://www.tariqamarneh.com/writing/grpc-contextvars-memory-leak).

## Use it as an agent skill

The skill walks an agent through the method: confirm the growth is real, reproduce one unit
of work, find what accumulates, trace what holds it, fix it by pattern, then prove it and add a
regression test. It installs the package below as needed.

```bash
git clone https://github.com/tariqamarneh/python-leak-hunter

# Claude Code: for you, or for one project
cp -r python-leak-hunter/skills/python-leak-hunter ~/.claude/skills/
cp -r python-leak-hunter/skills/python-leak-hunter your-project/.claude/skills/

# Codex: per repository
cp -r python-leak-hunter/skills/python-leak-hunter your-project/.agents/skills/
```

Then ask: *"Memory in our worker keeps growing. Find the leak."*

## Use it directly

```bash
pip install "python-leak-hunter @ git+https://github.com/tariqamarneh/python-leak-hunter"
```

No dependencies. Python 3.10+.

### 1. What accumulates?

Write a function that runs one unit of work (a request, a message, an agent turn), then:

```bash
leak-hunter check harness:one_request --include myapp. --iterations 30
```

```text
Objects accumulating across calls (2 windows x 30 calls):
  myapp.RunTrace  +1 per call  (window 1: +30, window 2: +30)
Next: why_alive('myapp.RunTrace') shows what keeps them alive.
```

The work runs in two windows after a warm-up, and only types that grow in **both** are
reported, so a cache filling up once is not a leak. Async functions work too. Add `--trace` to
also list allocation sites, which catches `bytes` and `str` that the object count cannot see.

### 2. What keeps it alive?

```bash
leak-hunter why harness:many_requests myapp.RunTrace
```

It walks referrers back to a root and groups instances that share a chain:

- `module X, global 'name'`: a module-level cache or registry.
- `variable v in f() at file:line`: a long-running function's local.
- `outside Python: ...`: nothing in Python refers to that object, yet it is alive, so native
  code holds it.

### 3. Keep it fixed

The `leak_check` pytest fixture is registered when the package is installed:

```python
def test_request_does_not_leak(leak_check):
    leak_check(handle_one_request, include=["myapp."], iterations=30)
```

```python
from leak_hunter import assert_no_leak, assert_no_leak_async, measure_growth, why_alive

report = measure_growth(handle_one_request, include=["myapp."])
print(report.format())
print(why_alive("myapp.RunTrace").format())
```

### Is it even a leak?

Sample memory in the running service and let `diagnose` separate real retention from
fragmentation and native allocations:

```python
import logging
from leak_hunter import MemorySampler, diagnose

sampler = MemorySampler(interval=60, on_sample=lambda s: logging.info("memory %s", s.to_json()))
sampler.start()
...
print(diagnose(sampler.samples))
```

```text
RSS grows +312.4 MB/h.
malloc heap in use grows +305.1 MB/h: memory is being retained, not fragmented. Find what accumulates (measure_growth), then what holds it (why_alive).
```

On glibc it reads `mallinfo2()` to split malloc's heap into bytes in use and bytes free. If
free space grows while in-use stays flat, the problem is fragmentation, and hunting objects
would be a waste of time.

## Try the demo

```bash
git clone https://github.com/tariqamarneh/python-leak-hunter && cd python-leak-hunter
uv run python examples/contextvars_leak/demo.py          # no gRPC needed
uv run --with grpcio python examples/grpc_cycle.py       # the real thing
```

## How it works

- **Growth:** counts live objects tracked by the garbage collector, by type, after a full
  collection, around two equal windows of calls.
- **Retention:** breadth-first search over `gc.get_referrers` from an instance to the nearest
  module global or running frame, skipping the tool's own references. Since Python 3.11,
  executing frames are not reported as referrers, so their locals are checked separately; an
  object held by a running function is not mistaken for native memory. A chain that ends at
  an object with no visible referrers is reported as held outside Python.
- **Memory:** RSS from `psutil` when installed, otherwise `/proc` or `ps`; heap stats from
  glibc's `mallinfo2()`; open file descriptors.

## Limits

- Finding instances by type only works for objects the garbage collector tracks: class
  instances and containers, not plain `str`, `bytes` or `int`. Use `--trace` for those.
- `why_alive` scans the heap at each step. Use it in a test or a staging process, not on a
  hot path in production.
- The heap split needs glibc 2.33+ (most Linux images). Elsewhere, `diagnose` falls back to the
  object count.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run mypy
```

MIT licensed. Built by [Tariq Amarneh](https://www.tariqamarneh.com).
