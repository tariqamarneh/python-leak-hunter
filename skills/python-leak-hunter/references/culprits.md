# Common leak patterns

Each pattern lists what the `why` chain usually looks like and the fix. Match on the chain,
not on the code that seems suspicious.

## 1. Clients, connections or SDKs created per request

**Chain:** the retained objects belong to a client library (`grpc`, `httpx.Client`,
`boto3` clients, database pools, LLM SDK clients), or end `outside Python` at one of its
types. Open file descriptors and SSL contexts grow with traffic.

**Fix:** one client per process, created lazily and reused. Guard creation with a lock if
it can race. If configurations differ, cache per configuration key:

```python
_clients: dict[tuple[str, str], SearchClient] = {}
_lock = threading.Lock()

def get_client(region: str, index: str) -> SearchClient:
    key = (region, index)
    client = _clients.get(key)
    if client is None:
        with _lock:
            client = _clients.get(key)
            if client is None:
                client = _clients[key] = SearchClient(region=region, index=index)
    return client
```

## 2. Request context captured by native code

**Chain:** ends `outside Python` at a library object holding a `contextvars.Context`, then
down through `hamt` nodes to your request objects. Example with gRPC:
`grpc._plugin_wrapping._Plugin._stored_ctx -> Context -> hamt -> ... -> RunTree`.

gRPC's auth plugin copies the current context when call credentials are created. Created
inside a request, that copy holds everything the request stored in context variables:
tracing run trees, callback managers, LangGraph config, the graph itself. When anything in
that context reaches the channel back, the cycle passes through C and is never collected.
Turning tracing off does not help if the run config alone references the graph.

**Fix:** create the client once per process (pattern 1), and construct it in an empty
context so it captures nothing:

```python
client = contextvars.Context().run(SearchClient, **config)
```

The same applies to anything that copies context at construction: some tracing and
instrumentation hooks, executors that propagate context, background threads started from a
request.

## 3. Unbounded caches

**Chain:** root is a module global dict, or a `functools` cache wrapper, holding entries
keyed per request, user or session.

- `functools.lru_cache(maxsize=None)` or `@cache` keyed by something unbounded.
- `@lru_cache` or `@cache` on a **method**: the cache keys include `self`, so every instance
  is kept alive.
- A module-level dict used as a cache with no eviction.

**Fix:** bound it (`maxsize`), give it a TTL, key it by something bounded, or use
`weakref.WeakValueDictionary` / `WeakKeyDictionary` when the cache should not own the values.
For methods, cache on the instance or use a module-level cache keyed by plain values.

## 4. Registries, listeners and handlers added per request

**Chain:** root is a module global or class attribute list or set holding callbacks,
`functools.partial` objects, bound methods or handler objects.

Examples: event listeners or signal handlers subscribed per request and never unsubscribed,
`logging.Handler`s added in a request path, `atexit.register` per call, plugin registries.

**Fix:** register once at startup, or unsubscribe in `finally`. Use `weakref.WeakMethod` for
bound-method listeners when the registry should not keep the owner alive.

## 5. Per-session state that is never evicted

**Chain:** root is an application-level dict keyed by session, conversation or thread id.

Examples: chat histories kept in memory per conversation; LangGraph's in-memory
checkpointer (`MemorySaver` / `InMemorySaver`) in a long-running service, which keeps every
thread's checkpoints for the life of the process.

**Fix:** a persistent checkpointer or store (Postgres, Redis, SQLite), or eviction by age
and count. Remove an unused in-memory checkpointer entirely.

## 6. Tracing and observability state

**Chain:** tracer or run-tree objects (`RunTree`, spans, callback handlers) held by a
tracer's map of open runs, or by a background upload queue.

Causes: runs started and never ended (an exception skips the end call), handlers attached
per request, an export queue that grows when the backend is slow.

**Fix:** end runs and spans in `finally` or with context managers, attach tracers once, bound
export queues, and sample high-volume traces.

## 7. asyncio tasks and futures

**Chain:** `_asyncio.Task` or `Future` objects held by a set or list of "background tasks",
or pending futures in a dict of waiters.

**Fix:** keep task references only while running (`task.add_done_callback(tasks.discard)`),
time out or cancel waiters, and remove entries from waiter maps in `finally`.

## 8. Exceptions kept around

**Chain:** a stored exception, then `__traceback__`, `traceback`, `frame`, and the frame's
locals: the whole request.

Examples: `self.last_error = exc`, collecting exceptions in a list for reporting.

**Fix:** store `repr(exc)` or a formatted string, or clear the reference
(`exc.__traceback__ = None`) once reported.

## 9. Closures, defaults and partials

**Chain:** `cell (closure variable)`, or a `function`'s `__defaults__`, or a
`functools.partial`, holding a large object, with the function itself held by a registry
(pattern 4) or a cache (pattern 3).

**Fix:** capture only the values needed (an id, not the whole request) or break the
registration.

## 10. Thread pools and thread-locals

**Chain:** `_thread._local` data, or thread-local caches in worker threads that live forever.

**Fix:** clear thread-local state at the end of each task, or avoid thread-locals for
per-request data (use explicit arguments or context variables scoped to the request).

## 11. Class-level mutable attributes

**Chain:** `class X.items`: a list or dict defined on the class body, shared by all instances
and growing with each one.

**Fix:** initialize mutable state in `__init__`.
