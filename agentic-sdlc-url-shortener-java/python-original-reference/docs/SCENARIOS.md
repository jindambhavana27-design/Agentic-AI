# Three Scenarios

Each shows decomposition, orchestration, and validation. All output below is from real
runs; nothing is illustrative.

```bash
python3 -m orchestrator run greenfield --risk-ceiling high
```

> **On `--risk-ceiling high`.** The default ceiling is `medium` and a release is assessed
> `high`, so the plain `run` command deliberately stops at the release gate with the
> approval **denied by the automation itself**. That is not a bug to work around — it is
> the control working. `--risk-ceiling high` is how an operator consents to unattended
> release for a demonstration.

---

## Scenario 1 — Greenfield: build a capability from nothing

**Requirement.** Link tagging and tag-based search: up to 8 tags per link, a normalisation
and validation rule, exact and prefix search ordered newest-first, and a stated latency
budget. No code for any of it exists.

### Decomposition

The requirements agent splits seven bullets into normalised requirements with acceptance
criteria, and asks about the non-functional dimensions the requirement leaves silent
(availability, retention, observability). Those answers are supplied as `clarifications` —
recorded as data, so the record shows what was assumed and who said so.

The architect maps requirements to components and reports coverage. The planner groups
work by owning component (the axis along which changes actually conflict), derives task
dependencies from component dependencies, and appends test and documentation tasks.

### Orchestration — the graph changes while it runs

Declared plan:

```
level 0   requirements
level 1   architecture      <- requirements
level 2   decompose         <- requirements, architecture
level 3   implement         <- decompose
level 4   docs-sync | security | test      (parallel)  <- implement
level 5   docs-verify       <- docs-sync
level 6   release           <- test, security, docs-verify   [approval]
```

What actually executes:

```
  decompose                14 nodes
  spec-T01 … spec-T05      ← injected at runtime by decompose
  implement                ← rewired to depend on all five
```

The planner returns five `Node` objects and a rewire instruction. `WorkflowGraph.expand`
applies both atomically and re-validates: an injection that would create a cycle is
rejected whole, and the run continues on the graph it had. The injected nodes are
`PROPOSE_ONLY`, so GOV-001 denies any of them that tries to mutate the workspace.

`implement` therefore waits on a synchronisation barrier that **did not exist when the run
started**.

### Validation

Real, not simulated:

- `test` runs the whole suite in a subprocess under the stdlib `trace` module: **238 tests,
  68.3% statement coverage**, gated on `pass_rate ≥ 1.0` and `coverage ≥ 55%`.
- `security` parses every module: dangerous calls, credential patterns, interpolated SQL,
  non-cryptographic randomness, plus HTTP-surface checks. **0 critical.**
- `docs-sync` regenerates `openapi.yaml` from the route table extracted from the source;
  `docs-verify` re-reads the file from disk and gates on `drift_count = 0`. Two nodes on
  purpose — the writer must not be the one that certifies its own work.
- `release` reads the recorded evidence. Missing evidence fails the check; absence is never
  read as success.

### Result

```
RUN greenfield -- SUCCEEDED
  nodes            : 14 total, 14 attempted, 14 succeeded
  success rate     : 100.0%  (first-pass 100.0%)
  gates            : 31 evaluated, 0 failed
  approvals        : 1 requested, 1 granted
  e2e latency      : 1.35s
  Audit: 53 events, chain valid
```

### What it looks like when something is actually wrong

The first execution of this scenario failed for real. The generated tag regex
`^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$` accepts a single character, so
`test_rejects_too_short` failed:

```
  test         failed     [1 of 238 test(s) failed]
  release      pending
Rollbacks
  triggered by test: 1 of 238 test(s) failed
RUN greenfield -- ROLLED_BACK
```

The test gate caught it, the run rolled the workspace back, and the release never ran. The
regex was fixed to `^[a-z0-9][a-z0-9-]{0,22}[a-z0-9]$`.

---

## Scenario 2 — Brownfield: change an existing system safely

**Requirement.** Operators cannot see analytics back-pressure, and expired links accumulate
with no way to reclaim them. Add a queue-depth gauge and an authenticated maintenance
endpoint that purges expired links and their aggregates.

### Codebase reasoning

`ImpactAnalysisAgent` parses the real tree with `ast` — it never imports the application,
because importing a module to inspect it runs arbitrary top-level code.

```
modules scanned      : 15
modules impacted     : 7   (blast radius 46.7%)
routes extracted     : 9
untested impacted    : 0
layering violations  : 0
composite risk score : 0.41
```

The blast radius is a reverse-dependency closure over the import graph, not a guess:
changing `service/storage/base.py` reaches `service.shortener` and `service.app`
transitively. The route table is read out of the router registrations, which is what makes
contract drift detectable later. Layering is checked against a declared layer order and
inversions are reported as findings.

The agent states what it structurally cannot see — dynamic imports, reflection, and that
test mapping is by import (reachability), not by executed lines.

### Orchestration

```
level 0   requirements
level 1   impact            <- requirements     [retry x3, PROPOSE_ONLY]
level 2   architecture      <- requirements, impact
level 3   decompose         <- architecture, impact
level 4   implement         <- decompose        [rollback, snapshot]
level 5   docs-sync | security | test           (parallel)
level 6   docs-verify
level 7   release                               [approval]
```

The change is **9 anchored edits across 6 files** — the storage port, both adapters, the
domain service, the router, and a new test module. Anchored patches fail loudly when the
anchor is missing or ambiguous, rather than guessing at a file that has moved on.

```
files_changed: 9   lines_changed: 260   dependencies_added: []
```

### Validation

- **228 tests, 68.3% coverage** — including 15 new tests for the purge semantics.
- The new route appears in the regenerated schema:

  ```yaml
  /api/v1/maintenance/purge:
    post:
      operationId: purge_expired
      security:
        - ApiKeyAuth: []
  ```

- The security agent re-extracts the route table **from the workspace**, not from context.
  A route table published by the impact stage predates the implementation; trusting it
  would have reviewed the API as it was before the change. (This was a real bug: the first
  version of the docs agent documented 9 routes when 10 existed.)

### Fault injection

```bash
python3 -m orchestrator run brownfield --risk-ceiling high --inject-fault
```

```
  impact       succeeded    0.82s   [2 retry]
  success rate : 100.0%  (first-pass 90.0%)
  retries      : 2 across 1 node(s)
  MTTR         : 0.82s
```

Two failures, exponential backoff (0.25s → 0.5s), recovery on the third attempt, and MTTR
measured from the first failure to eventual success. The reliability machinery is exercised
against the real engine rather than asserted in prose.

### Result

```
RUN brownfield -- SUCCEEDED
  nodes      : 10 total, 10 succeeded
  gates      : 29 evaluated, 0 failed
  approvals  : 1 requested, 1 granted
  Audit: 41 events, chain valid
```

---

## Scenario 3 — Ambiguous: a requirement that cannot responsibly be built

**Requirement, verbatim:**

> Make the shortener better for our marketing team. They need to see how links are doing
> and it should be fast. Add support for campaigns and make sure it scales. We should
> probably handle load better too.

### Refusing to guess

The detector finds unmeasurable terms with word-boundary matching, and flags six
non-functional dimensions the request never mentions:

| Question | Blocking |
| --- | --- |
| `Q-TERM-FAST` — "fast" has no latency target or percentile | yes |
| `Q-TERM-SCALABLE` — "scalable" to what request rate and data volume? | yes |
| `Q-TERM-HANDLE-LOAD` — what request rate, what concurrency? | yes |
| `Q-NFR-AVAILABILITY`, `Q-NFR-DATA_RETENTION`, … | advisory |

Ambiguity score **0.75**, over the 0.15 threshold, so the agent returns `NEEDS_INPUT` —
a first-class outcome, not a failure. Run it without answers and it stops:

```bash
python3 -m orchestrator run ambiguous --no-clarifications
```

```
  requirements   failed   clarification denied; cannot proceed on assumptions
  architecture   pending
  decompose      pending
```

Nothing downstream executes. Question ids are content-derived (`Q-TERM-FAST`, not `Q-V0`)
so a stored clarification set survives an unrelated edit to the requirement text.

### Non-linear re-planning

`stakeholder-review` runs **beside** `architecture`, not after it, and takes longer. When it
finishes it files a requirement nobody had written down: *campaign attribution must survive
link deletion*. That rewrites `requirements`, and the design is now built on inputs that no
longer exist.

```
Re-planning
  architecture        invalidated by stakeholder-review  (drift: requirements)
  decompose           invalidated by stakeholder-review  (drift: requirements)
  stakeholder-review  invalidated by architecture        (drift: requirements)
```

The third line is the mechanism proving itself: the reviewer had also read `requirements@1`,
so it too is stale and re-runs. Its second pass reads `@2`, produces byte-identical output,
the content hash is unchanged, no version is bumped, and the cascade stops. Convergence
comes from **idempotent agents plus content-addressed versions**, and the re-plan budget
catches anything that fails to converge.

`parallel gain 1.98x` — 1.45s of work in 0.73s wall clock.

### Controlled autonomy

Every node is `PROPOSE_ONLY`; GOV-001 denies any that produces a mutation. The deliverable
is a phased scope proposal with explicit non-goals and the recorded clarifications — which
is the correct engineering output for an ambiguous request. The proposal is gated on a human
decision, and its exit gate requires `questions_outstanding = 0`.

### Result

```
RUN ambiguous -- SUCCEEDED
  nodes         : 5 total, 5 succeeded
  success rate  : 100.0%  (first-pass 40.0%)   ← 3 nodes re-executed after re-planning
  re-plans      : 3 events over 3 nodes
  e2e latency   : 0.73s  (execution 1.45s)  →  parallel gain 1.98x
  Audit: 35 events, chain valid
```

The 40% first-pass rate is the honest number. Three of five nodes ran twice because their
inputs genuinely changed. A pipeline that reported 100% here would be one that shipped a
design contradicting its own requirements.

---

## Human-in-the-loop, end to end

```bash
python3 -m orchestrator run greenfield --approval deferred
```

```
run is paused. record decisions with:
  python3 -m orchestrator approve greenfield-20260802-104556-fb9b6c release
```

```bash
python3 -m orchestrator approvals
```

```
run=greenfield-… node=release risk=high
  release v1.2.0 to production
```

```bash
python3 -m orchestrator approve greenfield-20260802-104556-fb9b6c release --by release-manager --reason "evidence reviewed"
```

```bash
python3 -m orchestrator resume greenfield greenfield-20260802-104556-fb9b6c --approval deferred
```

```
RUN greenfield-20260802-104556-fb9b6c -- SUCCEEDED
  release    succeeded
```

The resume replays the journal to restore context without re-executing completed work, and
re-arms every unfinished node. (An earlier version did not re-arm, and reported the run
SUCCEEDED having silently never executed `release`. `_finalise` now treats any non-terminal
node as `HALTED`, and there is a regression test for it.)

## Verifying the trail

```bash
python3 -m orchestrator audit brownfield
```

```
events      : 41
chain valid : True
```

Each event carries the hash of its predecessor, so an edit anywhere in the file is
detectable. Tamper-evident, not tamper-proof — see the risks document.
