# When the growth is not Python objects

Use this when `diagnose` reports fragmentation or memory outside malloc, or when
`leak-hunter check` (with and without `--trace`) finds nothing while RSS keeps climbing.

`tracemalloc` and the object count only see Python's own allocations. C extensions (gRPC,
NumPy, database drivers, tokenizers, ML runtimes) allocate through malloc or mmap directly,
and their growth is invisible to them.

## Fragmentation: heap free space grows, in-use flat

Freed memory stays in the allocator instead of returning to the OS. It is common with many
threads (glibc creates up to 8 x cores malloc arenas) and with mixed allocation sizes.

Try, one at a time, and measure each:

1. `MALLOC_ARENA_MAX=2` in the environment: fewer arenas, usually much less fragmentation,
   small throughput cost.
2. jemalloc via `LD_PRELOAD` (Debian/Ubuntu package `libjemalloc2`; the library path varies
   by architecture, find it with `dpkg -L libjemalloc2`).
3. A diagnostic `malloc_trim(0)` call (`ctypes.CDLL("libc.so.6").malloc_trim(0)`): if RSS
   drops sharply right after, the memory was free but not returned. Use this to confirm, not
   as the fix.

## Native allocations: memray

[memray](https://github.com/bloomberg/memray) tracks native allocations with their Python
and C call stacks:

```bash
pip install memray
PYTHONMALLOC=malloc memray run --native -o leak.bin leak_harness_main.py
memray flamegraph --leaks leak.bin      # what was allocated and never freed
memray summary leak.bin
```

`leak_harness_main.py` should run the unit of work many times (`many_turns()`). With
`--leaks`, the flame graph shows only memory still allocated at exit, by call stack. A
running service can be attached to with `memray attach <pid>`.

## Long-running production processes: jemalloc heap profiles

For growth that only appears under real traffic over hours, profile the deployed process:

1. Preload a jemalloc built with profiling enabled. Start with
   `MALLOC_CONF=prof:true,lg_prof_interval:30,prof_prefix:/tmp/jeprof`. If jemalloc reports
   an invalid conf pair, it was built without profiling: build it from source with
   `--enable-prof`.
2. Let it dump profiles while traffic flows, and copy two of them (early and late) off the
   instance.
3. Diff them: `jeprof --show_bytes --base=early.heap $(which python) late.heap`. The top
   entries are the native code paths that grew in between, for example a client library's
   channel or connection creation.

Map what grew back to Python by asking which library owns that native code and where your
code creates its objects. Then return to step 4 of the main workflow: native growth is most
often pattern 1 (clients created per request).

## Containers

Container memory metrics can include page cache and other non-RSS memory. Compare the
platform's graph with the process's own RSS (the sampler logs it) before concluding.
