---
name: python-leak-hunter
description: Find and fix memory leaks in long-running Python processes (web servers, workers, async services, AI agents built with LangGraph or LangChain). Use when a Python service's memory keeps growing, it gets OOM-killed or restarted for memory, RSS climbs with traffic, or the user asks to find a memory leak, check whether code leaks, or add a leak regression test.
---

# Python leak hunter

Leaks are found by measurement, not by reading code. Plausible-looking suspects are usually
wrong, and a fix that is not measured is a guess. Work through these steps in order and keep
the numbers from each one; they go in the final summary.

1. Confirm the growth is real retention, and per what unit of work.
2. Reproduce one unit of work and find what accumulates.
3. Find what keeps it alive.
4. Fix it by pattern.
5. Prove the fix and lock it with a test.

Native memory (C extensions, fragmentation) branches off at step 1 or 2: see
[references/native-memory.md](references/native-memory.md).

## Tooling

The `python-leak-hunter` package provides the measurements. Install it into the **same
environment as the code under investigation**, because it imports and runs that code:

```bash
uv add --dev "python-leak-hunter @ git+https://github.com/tariqamarneh/python-leak-hunter"
# or: pip install "python-leak-hunter @ git+https://github.com/tariqamarneh/python-leak-hunter"
```

It has no dependencies. `psutil` is used for RSS when present.

## 1. Confirm and size the growth

Ask for, or collect, memory over time from the real process: RSS per process across hours,
next to traffic. If the service has nothing, add a sampler at startup and log one JSON line
a minute:

```python
import logging
from leak_hunter import MemorySampler

MemorySampler(interval=60, on_sample=lambda s: logging.info("memory_sample %s", s.to_json())).start()
```

Then classify with `leak_hunter.diagnose(samples)` (load the logged lines into
`MemorySample(**json.loads(line))`):

- **Heap in use grows** (glibc only): objects are retained. Continue with step 2.
- **Heap free space grows, in use flat**: fragmentation, not a leak. Go to native-memory.md.
- **Neither explains RSS**: allocations outside malloc. Go to native-memory.md.
- **Open file descriptors grow**: connections or files are created and never closed. Suspect
  clients built per request (step 4, pattern 1).

Size it: **MB per unit of work** (per request, message, session or agent turn), not just MB per
hour. If the cost per unit *rises* with traffic (each request retains more than the last, and
latency creeps up too), each new request is touching everything retained so far: think
listeners or callbacks that all run on every event. Compare process types: if a web process grows 10x faster per request than a worker
running the same library code, the leak is in the path only the web process runs.

Before reproducing locally, check the **exact library versions running in production**
(`pip freeze` inside the deployed image). A local lockfile that differs from the image can
send hours of measurement down the wrong path.

## 2. Reproduce one unit of work and find what accumulates

Write a small harness file, e.g. `leak_harness.py`, that runs exactly one unit of work through
the real code path. Fake the network edges (stub servers, fake LLM responses) but **keep real
client and SDK construction**: clients, credentials and tracing setup are the usual suspects,
and mocking them away hides the leak.

```python
# leak_harness.py
from myapp.agent import handle_message

def one_turn():
    handle_message({"text": "hello", "user": "u1"})

def many_turns():
    for _ in range(50):
        one_turn()
```

Run it (async functions work too). Use `--trace` from the start: the object count cannot see
`bytes`, `bytearray`, `str`, or dicts and lists that hold only plain values, and those are
often where the bytes are.

```bash
leak-hunter check leak_harness:one_turn --include myapp. --iterations 30 --trace
```

The check runs the work in two windows after a warm-up and only reports types that grow in
both, so caches filling up once are not reported. Reading the result:

- A type at **+1 per call** (or +N): that object is retained every time. Go to step 3 with it.
- Nothing reported with `--include`: run again without it. The retained object may belong to
  a library (`langsmith.run_trees.RunTree`, `grpc...`, `httpx.Client`).
- Allocation sites under `--trace` with no growing type: untracked data (bytes, buffers,
  plain-value dicts) is being kept. Look at the listed lines; the KiB per call should roughly
  match the per-unit cost from step 1.
- Small allocation sites that disappear with a fixed input, or with `--iterations` above a
  cache's `maxsize`, are a bounded cache filling up, not a leak. A harness that varies its
  input (realistic) will show these at first.
- Nothing at all: the growth is native or depends on real traffic patterns. See
  native-memory.md.

## 3. Find what keeps it alive

```bash
leak-hunter why leak_harness:many_turns myapp.graph.CompiledGraph
```

This runs the setup once, then traces a few of the instances it created back to a root and
groups identical chains. Prefer your own types over builtins (`list`, `dict`): `check`
suggests the first project type it found. Read the root line:

- `module X, global 'name'`: a module-level cache, registry or list is collecting them.
- `variable v in f() at file:line`: a long-running function or loop holds them in a local.
- `outside Python: nothing the garbage collector can see refers to this <T>`: a C extension
  holds `<T>`, and through it everything below. The first step names the object the native
  code keeps (for example `grpc._plugin_wrapping._Plugin._stored_ctx`). Identify which library
  owns that type and why it was created per request. If the chain below it can reach that
  library object again, the cycle passes through C and is never freed.

Read the chain from the bottom up: it is the path from your object to whatever keeps it.

In code, the same tools are `measure_growth(fn)`, `why_alive(type_or_name)` and
`why_alive(obj)`.

## 4. Fix by pattern

Match the chain to a known pattern in [references/culprits.md](references/culprits.md) and
apply its fix. Fix the cause, not the symptom:

- Never "fix" a leak with `gc.collect()` calls, periodic restarts or bigger memory limits.
  Those can be operational stopgaps; say so explicitly if you recommend one.
- Prefer creating clients once per process over closing them per request.
- Keep the diff small and explain the chain it breaks.

## 5. Prove it and lock it

1. Rerun `leak-hunter check ...`. It must report nothing for the types that grew before.
2. Add a regression test. The `leak_check` pytest fixture is registered automatically.
   Import the unit of work from the harness, or move it into the test file:

   ```python
   from leak_harness import one_turn

   def test_turn_does_not_leak(leak_check):
       leak_check(one_turn, include=["myapp."], iterations=30)
   ```

   If pytest cannot import the project package, set `pythonpath = .` in the pytest config.
   The check runs the work warm-up + 2 x iterations times, so side effects (rows written,
   listeners registered) add up; reset them in a fixture if other tests count them. In an
   async test, use `await leak_hunter.assert_no_leak_async(one_turn)`.

3. In production, compare MB per unit of work before and after on processes running the old
   and new code side by side over at least 30 minutes of real traffic. A flat line beats any
   argument.

## Report back

Summarize: the growth rate and unit of work, what accumulated (type, per call), the retention
chain, the fix, and the before/after numbers. Mention anything not verified.

## Mistakes to avoid

- Concluding from a graph "by eye" that disabling a feature changed nothing. Compare the
  per-unit slope.
- Treating one-off growth (warm caches, lazy imports, the first requests) as a leak, or a
  bounded cache that has not filled yet. If an LRU cache's limit is larger than the
  iterations, raise `--iterations` beyond it.
- Profiling with different library versions than production.
- Mocking out the client or SDK that is actually leaking.
- Stopping at the first plausible cause without confirming the numbers move.
