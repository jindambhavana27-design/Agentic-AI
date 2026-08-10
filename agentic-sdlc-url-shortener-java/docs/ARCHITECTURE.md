# Architecture

Two systems. `orchestrator/` drives an SDLC; `service/` is the software it drives.
They share no code and depend in one direction only — the orchestrator reads and edits the
service, the service knows nothing about the orchestrator.

---

## 1. Why this shape

The brief asks for an orchestration layer that is "non-linear, stateful, with governance,
rather than simple linear task chaining." Every structural decision below follows from
taking that seriously.

A linear chain is a list of steps where step *n+1* starts when step *n* ends. It cannot
express three things this problem actually needs:

1. **Work that genuinely overlaps.** Testing, security review, and documentation have no
   dependency on each other. Running them in sequence is not a simplification, it is a
   mistake — it triples the critical path and hides which one actually failed.
2. **Stages that stop being valid.** When a clarification lands after the design is done,
   the design is *wrong*, not merely old. A chain has no way to notice, because it never
   looks back at a finished step.
3. **Decisions that are not the agent's to make.** Releasing, adding a dependency, or
   accepting a large diff are organisational decisions. A chain can call a function named
   `approve()`; it cannot make approval a property of the execution model that no workflow
   author can bypass.

So the core is a **readiness scheduler over a mutable dependency graph** with a versioned
shared context, and governance is a layer the scheduler consults rather than a step in the
sequence.

---

## 2. Components

```
                    ┌───────────────────────────────────────┐
                    │  cli.py — operator surface            │
                    │  plan · run · approve · resume · audit │
                    └────────────────┬──────────────────────┘
                                     │
                    ┌────────────────▼──────────────────────┐
                    │  engine.py — readiness scheduler      │
                    │  admit → dispatch → react → re-plan   │
                    └──┬────────┬────────┬────────┬─────────┘
                       │        │        │        │
        ┌──────────────▼──┐ ┌───▼─────┐ ┌▼───────┐ ┌▼──────────────┐
        │ graph.py        │ │gates.py │ │policy  │ │approvals.py   │
        │ mutable DAG     │ │entry/   │ │.py     │ │auto·interactive│
        │ cycle-checked   │ │exit     │ │SEC/CHG/│ │·deferred       │
        │ runtime expand  │ │quality  │ │GOV     │ │risk ceiling    │
        └─────────────────┘ └─────────┘ └────────┘ └───────────────┘
                       │        │        │        │
        ┌──────────────▼────────▼────────▼────────▼─────────────────┐
        │ context.py — versioned blackboard + decision lineage      │
        └──────────────┬────────────────────────────────────────────┘
                       │
        ┌──────────────▼──────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐
        │ state.py            │ │audit.py  │ │metrics.py│ │replan.py   │
        │ event-sourced       │ │hash-     │ │derived   │ │staleness   │
        │ journal + projection│ │chained   │ │from      │ │→ invalidate│
        │ resumable           │ │JSONL     │ │journal   │ │→ cascade   │
        └─────────────────────┘ └──────────┘ └──────────┘ └────────────┘
                       │
        ┌──────────────▼────────────────────────────────────────────┐
        │ agents/ — requirements · architect · planner · impact ·   │
        │ implementer · tester · security · docs · release · spec   │
        └──────────────┬────────────────────────────────────────────┘
                       │ reads and edits
        ┌──────────────▼────────────────────────────────────────────┐
        │ workspace — a disposable copy of service/ tests/ docs/    │
        └───────────────────────────────────────────────────────────┘
```

| Module | Responsibility | Key decision |
| --- | --- | --- |
| `graph.py` | Nodes, edges, cycle detection, **runtime expansion** | Expansion is atomic and re-validated: an agent cannot inject a cycle |
| `engine.py` | Admission, dispatch, reaction, failure paths | Agents run concurrently; *all* governance runs on the scheduler thread |
| `context.py` | Versioned key/value blackboard, decision lineage | Versions are **content-addressed**, so an idempotent rewrite is not a change |
| `gates.py` | Entry (may this start?) and exit (is this acceptable?) | An agent never judges its own output |
| `policy.py` | Security, compliance, change control | Strongest verdict wins; a workflow author cannot waive a policy |
| `approvals.py` | Human checkpoints | Auto-approval has a **risk ceiling** it cannot exceed |
| `state.py` | Event journal + projection | In-memory state is derived, never authoritative |
| `audit.py` | Tamper-evident trail | Hash-chained JSONL, fsynced per event |
| `metrics.py` | Reliability numbers | Folded from the same events the auditor reads |
| `replan.py` | Staleness detection | Compares *observed* versions against current |
| `rollback.py` | Snapshots + compensating actions | Compensation is best-effort and never aborts mid-unwind |

---

## 3. Control flow

One scheduler iteration:

```
   ┌─ compute ready set ─────────────────────────────────────────┐
   │  deps satisfied?  (SUCCEEDED | SKIPPED | FAILED-but-CONTINUE)│
   └────────────────────────┬────────────────────────────────────┘
                            ▼
              ENTRY GATES ──fail──► blocking? ──yes──► FAILED
                            │                └──no───► SKIPPED
                            ▼ pass
              POLICY (pre) ──DENY──► FAILED
                            │
                     REQUIRE_APPROVAL ──► broker ──reject──► FAILED
                            │                      └─pending─► pause run
                            ▼ allow
              DISPATCH on the thread pool ──────────────────┐
                                                            │
   ┌────────────────────── react ◄───────────────────────────┘
   │
   ├─ FAILED ──► attempts left? ──yes──► backoff, requeue
   │             └─no──► CONTINUE | FALLBACK | ROLLBACK | SAFE_STOP | FAIL_RUN
   │
   ├─ NEEDS_INPUT ──► approval checkpoint (accept assumptions, or stop)
   │
   └─ OK ──► EXIT GATES ──fail──► treat as failure
             POLICY (post) ──DENY──► FAILED (not retried: it is deterministic)
                            REQUIRE_APPROVAL ──► broker
             ──► commit outputs to context
             ──► register compensation
             ──► expand graph (injected nodes + rewired barriers)
             ──► RE-PLAN: invalidate completed nodes whose inputs changed
```

Two properties of this loop matter more than the boxes:

**Governance is serialised.** Agent bodies run on a `ThreadPoolExecutor`; every state
transition, gate evaluation, policy check, and approval happens on the scheduler thread.
So the journal is a faithful serial history and there is no interleaving to reason about
in the part of the system where correctness is hardest to recover.

**The exit path is not the agent's.** An agent returning `OK` has *proposed* success. Gates
and policy decide whether it is accepted. `TestAgent` cannot pass a node when the suite
failed, and `ImplementationAgent` cannot ship a diff over the change budget without a
human.

---

## 4. Re-planning: the part a chain cannot do

Every context read records the version observed:

```
requirements v1 ──read──► architecture   (observed: requirements@1)
               ──read──► stakeholder-review
                              │
                              └── writes requirements v2
                                          │
                    architecture observed @1, current is @2  ──► STALE
```

`replan.py` compares observed versions against current ones for every completed node —
**not only descendants of the trigger**. Position in the graph is not what makes a node
stale; reading an input that has since changed is. That distinction is exactly what makes
the parallel-review case work, and it is the case a linear pipeline gets wrong.

Invalidation cascades to descendants (their inputs are about to change too), the affected
nodes return to `PENDING`, and the scheduler picks them up.

Two things stop this from looping:

- **Content-addressed versions.** Rewriting a key with an identical value does not bump the
  version, so an idempotent re-run costs nothing downstream.
- **A re-plan budget** (`max_replans_per_node`). Past the cap the run safe-stops for a human
  rather than spinning. Non-convergence is a signal, not something to absorb.

Observed in the ambiguous scenario: 3 invalidations, converging without hitting the budget.

---

## 5. Governance model

### Gates vs. policy

They answer different questions and have different owners:

- A **gate** asks *is this work correct?* — owned by the workflow author, scoped to a node.
- A **policy** asks *is this work allowed?* — owned by the organisation, applies everywhere.

A workflow author can choose their gates. They cannot switch off `SEC-001`.

### The policy set

| ID | Category | Rule |
| --- | --- | --- |
| SEC-001 | security | No credential-shaped strings in generated artifacts → **DENY** |
| SEC-002 | security | Critical security finding → **DENY**; high → **REQUIRE_APPROVAL** |
| SEC-003 | security | Artifact paths must stay inside the workspace → **DENY** |
| CHG-001 | change control | Diff beyond the review budget → **REQUIRE_APPROVAL** |
| CHG-002 | change control | New runtime dependency → **REQUIRE_APPROVAL** |
| CHG-003 | change control | Destructive migration without a rollback script → **DENY** |
| GOV-001 | compliance | A `PROPOSE_ONLY` node that produced mutations → **DENY** |
| GOV-002 | compliance | Release without test/security/docs evidence → **DENY** |
| GOV-003 | compliance | Implementation artifact with no requirement id → **WARN** |

Verdicts combine by strength: `ALLOW < WARN < REQUIRE_APPROVAL < DENY`.

### Autonomy levels

Declared per node, enforced by GOV-001:

| Level | Meaning | Used by |
| --- | --- | --- |
| `PROPOSE_ONLY` | Analyse; never mutate | impact, task specs, docs-verify, every ambiguous-scenario node |
| `ACT_WITH_APPROVAL` | Mutate only after a human decision | release |
| `ACT_AND_REPORT` | Mutate freely, surface everything | requirements, architecture, planning, implementation |
| `AUTONOMOUS` | Mutate within policy | test, security |

### Approval brokers

| Broker | Behaviour | Fits |
| --- | --- | --- |
| `auto` | Approves up to a **risk ceiling**, escalates above it | CI |
| `interactive` | Prompts; **denies** when stdin is not a TTY | Local operation |
| `deferred` | Persists the request, pauses the run, resumes on decision | Real change control |

The default ceiling is `medium` and a release is assessed `high`, so **`python3 -m
orchestrator run greenfield` deliberately stops at the release gate.** That is the
system working. `--risk-ceiling high` is how you consent to unattended release in a demo.

---

## 6. Reliability

| Control | Mechanism |
| --- | --- |
| Retry | Per-node `RetryPolicy`, exponential backoff, only for outcomes declared retryable |
| Fallback | `FailureAction.FALLBACK` swaps in a declared fallback agent |
| Rollback | Workspace snapshot + reverse-order compensations (saga) |
| Safe-stop | Explicit, or automatic after N consecutive failures |
| Timeout | Per-node deadline; the node fails (the thread is abandoned — see the risks doc) |
| Resume | Journal replay restores context and re-arms unfinished nodes |
| Liveness | Iteration ceiling and re-plan budget bound every loop |

A policy **denial** is never retried — it is deterministic, and re-running it would only
produce the same violation more slowly.

---

## 7. Metrics, and how they are kept honest

Everything is folded from the journal, so the numbers cannot disagree with the audit trail
and no agent can inflate its own. Three definitions are stated because they are commonly
fudged:

- **Success rate** — succeeded ÷ *ruled on*. A node blocked by an entry gate counts in the
  denominator even though its agent never ran; excluding it would let a run that failed at
  a gate report 100%.
- **MTTR** — mean time from a node's first failed attempt to its eventual success. Nodes
  that never recovered are excluded and reported separately as `unrecovered_failures`.
  Folding them in as zero or infinity are both lies.
- **Execution latency** — wall clock minus time parked awaiting a human. Otherwise the
  metric measures reviewer availability.

Re-plan re-executions are excluded from `retry_count`: an invalidation legitimately buys a
fresh attempt, and counting it as a retry would make healthy re-planning look unreliable.

---

## 8. The service

Layered, dependencies pointing one way only:

```
server.py     stdlib HTTP adapter (thin by design — swapping to ASGI is contained)
   ▼
app.py        routing, middleware chain, error mapping
   ▼
shortener.py  domain rules
   ▼
storage/      LinkStore port → SQLite | in-memory adapters
```

`app.py` exposes `handle(Request) -> Response`, so the whole API is testable without a
socket. Nothing below `app.py` imports anything HTTP-related. The impact agent verifies
this layering by parsing imports and reports inversions as findings.

Decisions worth defending:

| Decision | Why | Cost |
| --- | --- | --- |
| Random base62 codes, not a counter | A sequential keyspace makes the entire corpus enumerable | One existence check per creation, plus collision retries |
| Soft delete, never reissue | A reused code silently pointing somewhere new is a redirect attack | The links table grows without bound |
| Pre-aggregated analytics | Raw click rows grow without bound and we only serve aggregates | Per-click detail is unrecoverable |
| Lazy expiry on read | No sweeper process, no clock skew between writer and reader | Expired rows occupy storage until purged |
| Async analytics queue | A redirect must never be slowed or failed by analytics | Click counts are lossy under sustained back-pressure |
| Zero dependencies | The reviewer runs it immediately; no supply chain to audit | A hand-written HTTP layer and metrics registry |

---

## 9. Extension points

- **New scenario** — a module in `workflows/` exposing `build()` and `default_inputs()`.
  The registry picks it up; the engine does not change.
- **New agent** — subclass `Agent`, declare `reads`/`writes`, implement `execute`. The
  declared reads are what drive re-planning, so they are load-bearing, not documentation.
- **New policy** — subclass `Policy` (or use `CustomPolicy`) and register it. It applies to
  every workflow immediately.
- **LLM-backed agents** — `Agent.execute` is the seam. An agent that calls a model returns
  the same `AgentResult` and is subject to the same gates and policies. That containment is
  the point: the orchestration layer's guarantees do not depend on what is inside an agent.
