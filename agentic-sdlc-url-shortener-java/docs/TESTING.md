# Testing Approach

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m unittest discover -s tests_orchestrator -t .
```

**416 tests, no third-party dependencies, ~13 seconds.**

| Suite | Tests | Covers |
| --- | ---: | --- |
| `tests/` | 210 | The service: validation and SSRF controls, domain rules, both storage adapters, HTTP behaviour, rate limiting, analytics buffering, configuration, and a real socket round-trip |
| `tests_orchestrator/` | 206 | The engine: graph and context, scheduling and failure paths, governance, the SDLC agents, and the three workflows |

The suites are separate because `tests/` is copied into every run workspace and executed by
the test agent. Keeping the engine's own tests out avoids the orchestrator testing itself
recursively inside its own runs.

---

## What is actually being asserted

### The service

**One contract, both adapters.** `StoreContractMixin` runs the same 25 assertions against
`MemoryLinkStore` and `SQLiteLinkStore`. Divergence between the store used in tests and the
store used in production would make the rest of the suite prove nothing — including subtler
things than CRUD, like whether idempotency keys are scoped per owner and whether cursor
pagination covers every row exactly once.

**Transport without a socket.** `Application.handle(Request) -> Response` is called
directly, so the HTTP suite is fast and has no port conflicts. `ServerIntegrationTests` then
binds a real ephemeral port and drives the stdlib adapter over HTTP, so the thin translation
layer that direct calls skip is still covered.

**Adversarial input, not happy paths.** The validation suite asserts the things that
actually bite: `169.254.169.254` (cloud metadata), `[::1]`, `10.0.0.5`, `db.internal`,
`localhost.` with a trailing dot, `javascript:` and `file:` schemes, embedded credentials,
CR/LF in a URL that would become a header injection, and `True` passed as a TTL (a bool is
an `int` in Python — accepting it silently sets a one-second expiry).

**Determinism where it matters.** The rate limiter takes an injectable clock, so refill,
capacity capping, and idle-bucket reclamation are asserted exactly rather than with
`sleep()`. The analytics recorder runs synchronously in tests, because an async writer would
deliver the click after the assertion that reads it.

**Failure paths are tested as first-class behaviour.** A store that raises on every write
must not break a redirect. An unhandled exception in a handler must return an opaque 500
with the internal message absent from the body — asserted by searching the serialised
response for the secret string.

### The orchestrator

Engine tests use trivial in-memory agents. That keeps them about *orchestration* rather than
about whether a particular scanner found a particular finding:

| Property | How it is asserted |
| --- | --- |
| Real parallelism | Two 0.25s siblings; assert their execution *windows overlap* and total wall clock < 0.45s — not merely that both ran |
| Barrier semantics | A join node's start index in the log is after both parents' end indices |
| Parallelism limit | Four 0.1s nodes at `max_parallelism=1` must take ≥ 0.35s |
| Retry and backoff | An agent failing a fixed number of times; assert call count, retry count, and that a 0.3s backoff shows up in e2e latency |
| Timeout | A 2s agent with a 0.2s deadline fails with "timed out" |
| Rollback ordering | Compensations recorded into a list; assert reverse order |
| Partial rollback | One compensation raises; the others still run and the report is marked unclean |
| Snapshot restore | Real files written to a temp workspace, modified, then a downstream failure; assert the modified file is restored and the added file is gone |
| Re-planning | A reader observes `["v1", "v2"]` after a parallel writer changes the key |
| Convergence | An identical rewrite produces **zero** re-plans |
| Budget | A writer that changes the value every time halts instead of looping |
| Graph expansion | Injected nodes execute and the rewired barrier appears in `depends_on` |
| Atomicity | A cycle-creating expansion leaves neither the node nor the edge behind |
| Audit integrity | Mutate one event's payload in place; assert `verify_chain` reports the exact break index |
| Journal replay | Rebuild `RunState` from the file; assert the status map matches the live run |

Agent tests run against a **real workspace copy**: the impact agent's blast radius, route
extraction, and auth flags are asserted against this repository's actual source, so the
tests fail if the analysis stops matching the code it analyses.

---

## Coverage measurement

The test agent runs the suite in a subprocess under the stdlib `trace` module. Executed
lines come from the tracer; the denominator is the set of statement lines found by parsing
each module with `ast`, excluding `def`/`class` headers and docstrings, which execute once
at import and would inflate the number without measuring anything.

**Measured: 68.3% statement coverage** across `service/`.

Stated plainly, because coverage figures are routinely oversold:

- This is **statement coverage, not branch coverage**. A line with two outcomes counts once.
- The `ast`-derived denominator is an approximation of what `coverage.py` would report.
- A subprocess is used so the orchestrator's own threads are not traced.
- Coverage is a **floor, not a goal**. The release gate requires ≥ 55%; the low-coverage
  modules are `server.py` (12% — the socket adapter, exercised by one integration test) and
  `errors.py`/`models.py` (28% — mostly `to_dict` branches). Those are named in the test
  report rather than hidden.

---

## What is deliberately not tested

Stating these is more useful than pretending the suite is complete.

| Not covered | Why | What would be needed |
| --- | --- | --- |
| Concurrency at load | The store is asserted thread-safe by construction (locks, thread-local connections) but not under contention | A load generator plus SQLite `BUSY` fault injection |
| Multi-replica behaviour | The rate limiter is per-process by design; there is nothing to test in one process | A second replica and a shared counter |
| Crash mid-write | Journal and audit are fsynced per event, but torn writes are not simulated | Process kill under `fault-injection` on the write path |
| LLM-backed agents | None exist; agents are deterministic | Recorded fixtures plus a nondeterminism budget per agent |
| Timed-out agent side effects | A timed-out thread is abandoned and may still be running | Cooperative cancellation in the agent contract |
| Long-horizon re-planning | Convergence is asserted over 3–4 re-plans, not hundreds | A property test over random write schedules |
| Performance | One latency budget is asserted (tag search < 50 ms); nothing else is benchmarked | A benchmark harness and a baseline to regress against |

---

## Test design conventions

- **One behaviour per test**, named as a sentence about that behaviour. `test_expired_link_is_gone_not_missing` says what it protects; `test_resolve_2` would not.
- **Comments explain the threat, not the code.** Where a test guards something non-obvious — bool-as-int TTL, referrer leakage, a skipped scan being treated as a passed scan — the comment says why it matters.
- **No mocks of the system under test.** Mocking appears twice, both times to force a
  genuinely unreachable state: code-generation collisions and a store that always raises.
- **Time is injected, never slept on**, wherever the assertion is about a rule rather than
  about real concurrency.
