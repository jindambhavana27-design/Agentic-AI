# Risks, Trade-offs, and Failure Modes

What can go wrong, what is guarded, and what was consciously given up.

---

## 1. Orchestration risks

### An agent does something it was not authorised to do

Agents write files and run subprocesses. An agent that is wrong — or a change plan that is
malicious — is the highest-impact failure in the system.

| Guard | Mechanism |
| --- | --- |
| Workspace isolation | Every run executes in a disposable copy; the checkout is never touched |
| Path confinement | Checked twice — in `_apply` before writing, and by SEC-003 after |
| Autonomy levels | GOV-001 denies a `PROPOSE_ONLY` node that produced mutations |
| Artifact confinement | `_persist_artifacts` refuses paths escaping the artifacts directory |
| Change budget | CHG-001 escalates a diff too large to review |
| Secret scanning | SEC-001 denies credential-shaped strings in generated output |

**Residual risk.** Nothing sandboxes an agent at the OS level. An agent that shells out can
do anything the orchestrator's user can. The workspace boundary is a *convention enforced in
Python*, not a kernel boundary. Production would run agents in a container with a read-only
root and no network.

### The system reports success it did not earn

The most dangerous failure available to this class of system. Three separate guards:

- **The agent does not judge itself.** Exit gates and policy decide acceptance.
  `TestAgent` returning `OK` is not what passes the node — `pass_rate ≥ 1.0` is.
- **Absence is never success.** `ReleaseRequiresEvidence` denies a release whose evidence
  keys are missing; every `ReadinessCheck` fails when its report is absent.
- **Unfinished ≠ succeeded.** `_finalise` reports `HALTED` if any node is non-terminal.

This last one was a real bug: a resumed run reported `SUCCEEDED` having never executed
`release`, because the node was parked in `AWAITING_APPROVAL` and therefore unschedulable.
Fixed by re-arming unfinished nodes on resume, plus the non-terminal guard. There is a
regression test for each half.

### Re-planning never converges

An agent whose output changes on every run invalidates its readers forever.

**Guards.** Content-addressed versions (identical rewrite ⇒ no version bump ⇒ no cascade);
a per-node re-plan budget that safe-stops rather than spins; an iteration ceiling as a
liveness backstop.

**Residual risk.** Convergence depends on **agent idempotence**, which the engine cannot
enforce. It is stated in the `Agent` contract and demonstrated in `StakeholderReviewAgent`,
which checks whether its derived requirement is already present before adding it. A
non-idempotent agent will exhaust the budget and halt — noisy, but not silently wrong.

### A timed-out agent keeps running

Python has no safe thread cancellation. A node past its deadline is failed and its worker
**abandoned**; the thread may still be writing files.

**Guard.** The failure is recorded, the late result is discarded, and agents are required to
be idempotent. **Residual risk is real.** A timed-out `ImplementationAgent` could still be
mid-write while rollback restores the snapshot. The correct fix is process isolation per
agent with a killable child — deliberately out of scope here, and called out rather than
papered over.

### Deadlock or a silently stalled run

**Guards.** The scheduler distinguishes "nothing to do" from "nothing runnable": if no node
is ready, none is in flight, and unfinished work remains, it halts with an explicit reason
instead of hanging. Iteration ceiling, re-plan budget, and per-node timeouts bound every
loop.

### The audit trail is edited

**Guard.** Each event carries its predecessor's hash and is fsynced on write, so any
insertion, deletion, or edit is detectable by `verify_file`.

**Residual risk.** Tamper-**evident**, not tamper-**proof**. Anyone who can rewrite the file
can recompute the whole chain. Real integrity needs an append-only sink outside the machine —
a WORM bucket, a signed transparency log, or periodic hash anchoring.

### Automation approves its own changes

**Guards.** `AutoApprovalBroker` has a risk ceiling it cannot exceed; releases are assessed
`high` so the default `medium` ceiling stops them. `InteractiveApprovalBroker` **denies** when
stdin is not a TTY — an unattended pipeline cannot fall through to approval. `DeferredApprovalBroker`
persists decisions with an attributed decider.

**Residual risk.** The deferred store is a local JSON file with no authentication. Anyone
with filesystem access can approve anything. Production needs a real approval service with
identity and non-repudiation.

### The analysis lies because it is stale

`route_table` published by the impact stage predates the implementation.

**Guard.** Post-implementation agents re-extract from the workspace and treat context as a
cross-check; the divergence is reported as a finding. This was a real bug — the docs agent
documented 9 routes when 10 existed, and the new endpoint shipped undocumented.

**Residual risk, general form:** any agent trusting context written before a mutating stage
has the same problem. The versioned context makes it *detectable* but does not make it
impossible.

---

## 2. Where the agents are weak

### Ambiguity detection is lexical

`RequirementsAgent` uses a rule set, not a model. Deliberate: deterministic, explainable,
and incapable of hallucinating a requirement nobody wrote.

**What it misses.** Semantic ambiguity with no vague vocabulary. *"Links should expire after
30 days"* — from creation or last access? The detector sees a precise number and passes it.
Only a domain expert catches that.

**What it over-flags.** Fixed two false-positive classes during development: substring
matches (`many` inside `Germany`) and interrogative usage (*"how many events are buffered"*).
Word-boundary matching and an interrogative exclusion now handle both. Others certainly
remain. **A noisy detector gets switched off, which is worse than a narrow one** — hence the
conservative rule set and the escalation to a human rather than an automatic resolution.

### Static analysis cannot see everything

The impact and security agents parse source. Invisible to them: dynamic imports,
`getattr` dispatch, reflection, runtime configuration. Test-to-module mapping is by import —
it proves *reachability*, not execution. Every agent lists its own limits in its report
rather than implying completeness.

The SQL-interpolation check fires at the call site only, so hoisting a query into a local
variable defeats it. That is documented in the agent's `limits` field: **it is a lint, not a
proof.**

### The security scanner has no CVE data

It reviews source. It does not check dependency versions against advisories. The project has
zero third-party dependencies, so there is nothing to check today — but that is a property of
this codebase, not a capability of the scanner. Adding a dependency needs a real SCA tool
alongside CHG-002.

---

## 3. Service risks

| Risk | Guard | Residual |
| --- | --- | --- |
| **SSRF via redirect target** | Scheme allowlist, private/loopback/link-local/reserved IP rejection, blocked internal suffixes, FQDN requirement, credential rejection | **DNS rebinding.** Validation happens at creation; a hostname resolving publicly then to `10.0.0.1` defeats it. Correct fix: resolve-and-pin at redirect time, or an egress proxy |
| **Open redirect abuse** | `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, soft delete so a code can be retired | The service is a redirector; that is the product. Mitigation is takedown speed and abuse reporting, not prevention |
| **Header injection** | Control characters rejected in URLs; `X-Request-Id` sanitised to `[A-Za-z0-9._-]{≤64}` | — |
| **Code enumeration** | 7 random base62 characters (3.5×10¹²) from the CSPRNG, never sequential | An attacker with unlimited requests can still sample; rate limiting is the real control |
| **Timing attack on API keys** | `hmac.compare_digest` against every configured key | Key count is observable through total comparison time — negligible |
| **Key leakage into storage** | The owner is a SHA-256 prefix, never the key; credential-shaped run inputs are redacted from journal and audit | — |
| **Rate-limiter memory exhaustion** | Idle-bucket sweep plus a hard cap with LRU eviction | — |
| **Rate limiting across replicas** | *None.* Per-process by design | **N replicas permit N× the configured rate.** Blocking for horizontal scale; needs Redis or consistent-hash routing |
| **Analytics loss** | Bounded queue, drop-on-full, `analytics_events_dropped_total` counter | **Clicks are lost under sustained back-pressure.** Accepted: a slow or failed redirect is worse than an undercount |
| **Unauthenticated writes** | Startup **fails** if auth is required and no keys are set | — |
| **Storage growth** | Pre-aggregated analytics; soft delete keeps link rows forever | The links table grows without bound. The brownfield purge endpoint addresses expired links; deleted-link tombstones are permanent by design |

---

## 4. Trade-offs, and what they cost

### Zero third-party dependencies

**Bought:** the reviewer runs `python3 -m unittest` and it works. No install, no lockfile, no
supply chain, no version drift. For a system whose own policy engine escalates on new
dependencies, having none is the consistent position.

**Paid:** a hand-written HTTP layer (~250 lines) instead of FastAPI, a hand-written metrics
registry instead of `prometheus_client`, a 40-line YAML path reader instead of PyYAML, and
statement coverage via `trace` instead of `coverage.py`. Each is less capable than the
library it replaces. All are behind interfaces, so replacement is contained.

### `ThreadingHTTPServer`, not ASGI

Adequate for an I/O-bound redirect service at the stated scale. Thread-per-connection does
not reach the concurrency an event loop would, and the GIL bounds CPU work. `app.py` is
transport-neutral, so the adapter is the only thing that would change.

### SQLite, not Postgres

Zero operational surface, WAL for concurrent reads, real transactions. Single-writer, single
-host — no replication, no failover. `LinkStore` is the seam; a Postgres adapter is additive.

### Threads, not processes, for agents

Shared memory makes context trivially available and keeps the engine simple. The cost is the
un-killable timeout described above, no CPU parallelism, and no fault isolation — an agent
that segfaults the interpreter takes the run with it.

### The graph is mutable at runtime

Genuine decomposition: a planner produces schedulable nodes, not a document about work.
The cost is that the executed graph differs from the declared one, so `plan` shows the
starting shape and the report shows what actually ran, with every injection recorded in the
audit trail as `nodes_injected`.

### Deterministic agents, not LLM-backed ones

**Bought:** the run is reproducible, the tests assert real behaviour, and the demonstration
of *orchestration* is not confounded by model variance. Every signal the engine gates on is
measured — a real test run, a real parse, a real diff.

**Paid:** the agents cannot handle inputs outside their rule sets. This is the honest
boundary of the prototype. `Agent.execute` is the seam: an LLM-backed agent returns the same
`AgentResult` and is subject to the same gates and policies. That containment is the design
claim — the orchestration layer's guarantees do not depend on what is inside an agent. It is
argued, not demonstrated, and that distinction matters.

---

## 5. What I would do next, in order

1. **Process-isolate agents** — a killable child per agent closes the timeout gap, the only
   residual risk here that can corrupt a workspace.
2. **Externalise the audit sink** — append-only storage off the machine turns tamper-evident
   into tamper-resistant.
3. **A real approval service** — identity, non-repudiation, and expiry, replacing the local
   JSON file.
4. **Shared-state rate limiting** — the one thing blocking horizontal scale.
5. **Resolve-and-pin at redirect time** — closes the DNS-rebinding hole in SSRF validation.
6. **One LLM-backed agent behind the existing gates** — the cheapest way to test whether the
   containment claim in §4 actually holds.
