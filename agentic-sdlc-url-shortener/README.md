# Agentic Software Engineering System — URL Shortener

Two things live in this repository:

1. **`service/`** — a production-shaped URL shortener: creation with custom aliases and
   idempotency, safe redirects, expiry, click analytics, rate limiting, API-key auth,
   structured logging, and Prometheus metrics.
2. **`orchestrator/`** — an **agentic SDLC orchestration layer** that drives that service
   through requirements, design, decomposition, implementation, testing, security review,
   documentation, and release — as a stateful dependency graph with gates, policy
   guardrails, human approval checkpoints, bounded retries, rollback, and dynamic
   re-planning.

The orchestrator is the point of the exercise; the shortener is the system it operates on.

**Zero third-party dependencies.** Python 3.8+ and the standard library. Nothing to install.

---

## Quick start

Run the whole thing:

```bash
python3 -m unittest discover -s tests -t .
```

Start the service:

```bash
SHORTENER_API_KEYS=dev-key-1 python3 -m service.server
```

Create and follow a link:

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/links -H 'X-API-Key: dev-key-1' -H 'Content-Type: application/json' -d '{"url":"https://example.com/a-very-long-address","alias":"demo"}'
```

```bash
curl -i http://127.0.0.1:8080/demo
```

Run the orchestrator against a scenario:

```bash
python3 -m orchestrator run greenfield
```

```bash
python3 -m orchestrator run brownfield
```

```bash
python3 -m orchestrator run ambiguous
```

Each run works in a **disposable copy** of the project. Your working tree is never
modified.

---

## The orchestration layer

`python3 -m orchestrator plan greenfield` prints the graph before anything executes:

```
workflow: greenfield
  level 0 (sequential):
    - requirements               requirements
  level 1 (sequential):
    - architecture               architecture   <- requirements
  level 2 (sequential):
    - decompose                  planning       <- requirements, architecture
  level 3 (sequential):
    - implement                  implementation <- decompose
  level 4 (parallel):
    - docs-sync                  documentation  <- implement
    - security                   security       <- implement
    - test                       testing        <- implement [retry x2, compensable]
  ...
  level 6 (sequential):
    - release                    release        <- test, security, docs-verify [approval]
```

At runtime the planner **injects** one specification node per decomposed task and rewires
`implement` to depend on all of them, so the graph the run finishes with is not the graph
it started with.

| Command | What it does |
| --- | --- |
| `plan <scenario>` | Print the DAG, parallel levels, and gates without executing |
| `run <scenario>` | Execute; `--approval auto\|interactive\|deferred`, `--dry-run`, `--inject-fault` |
| `approvals` | List human decisions the run is waiting on |
| `approve` / `reject` `<run> <node>` | Record a decision out of band |
| `resume <scenario> <run>` | Continue a run that paused for approval |
| `audit [scenario]` | Verify the tamper-evident hash chain |
| `report <run>` | Print the reliability metrics from a stored run |
| `policies` | List the active security / compliance / change-control guardrails |

Full design rationale is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the three
scenarios are walked through in [docs/SCENARIOS.md](docs/SCENARIOS.md).

---

## API

All management endpoints require an `X-API-Key` header. Redirects are public.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/links` | yes | Create a short link (optional `alias`, `ttl_seconds`, `metadata`) |
| `GET` | `/api/v1/links` | yes | List links, keyset-paginated via `limit` and `cursor` |
| `GET` | `/api/v1/links/{code}` | yes | Link metadata |
| `GET` | `/api/v1/links/{code}/stats` | yes | Click analytics over a `window` (`7d`, `24h`, `2w`) |
| `DELETE` | `/api/v1/links/{code}` | yes | Soft-delete; the code is never reissued |
| `GET` | `/{code}` | no | Resolve and redirect |
| `GET` | `/healthz` `/readyz` | no | Liveness and readiness |
| `GET` | `/metrics` | no | Prometheus exposition |

Status codes carry meaning: **404** the code was never issued, **410** it existed and is
expired or deleted, **409** the alias is taken, **429** rate limited (with `Retry-After`).
Errors share one shape and always carry the `X-Request-Id` that appears in the logs:

```json
{"error": {"code": "unsafe_url", "message": "url host is not publicly routable", "request_id": "9f2c..."}}
```

The machine-readable contract is [docs/openapi.yaml](docs/openapi.yaml), regenerated from
the route table by the documentation agent and independently re-verified on every run.

---

## Configuration

Every setting is an environment variable read once at startup (`service/config.py`).

| Variable | Default | Notes |
| --- | --- | --- |
| `SHORTENER_HOST` / `SHORTENER_PORT` | `127.0.0.1` / `8080` | |
| `SHORTENER_BASE_URL` | `http://127.0.0.1:<port>` | Used to build returned short URLs |
| `SHORTENER_DB_PATH` | `shortener.db` | `:memory:` selects the in-process store |
| `SHORTENER_API_KEYS` | *(none)* | Comma-separated; **required** unless auth is off |
| `SHORTENER_REQUIRE_AUTH` | `true` | Set `false` for local development only |
| `SHORTENER_CODE_LENGTH` | `7` | Random base62; 62⁷ keyspace |
| `SHORTENER_REDIRECT_STATUS` | `302` | `301` is cached by browsers — see the trade-offs doc |
| `SHORTENER_DEFAULT_TTL_SECONDS` | *(none)* | Per-link `ttl_seconds` overrides it |
| `SHORTENER_ALLOW_PRIVATE_HOSTS` | `false` | **Leave off.** Enabling it makes the redirector an SSRF pivot |
| `SHORTENER_RATE_LIMIT_ENABLED` | `true` | Token bucket, per API key or source address |
| `SHORTENER_RATE_LIMIT_CAPACITY` / `_REFILL` | `60` / `1.0` | Burst size and tokens per second |
| `SHORTENER_ANALYTICS_ENABLED` | `true` | Off means redirects are not counted |
| `SHORTENER_LOG_LEVEL` | `INFO` | Logs are single-line JSON |

Starting with `SHORTENER_REQUIRE_AUTH=true` and no keys is a **startup failure**, not a
warning — a service that silently serves unauthenticated writes is worse than one that
refuses to boot.

---

## Testing

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m unittest discover -s tests_orchestrator -t .
```

The first suite covers the service (validation and SSRF controls, domain rules, both
storage adapters against one shared contract, HTTP behaviour, rate limiting, analytics
buffering, and a real socket round-trip). The second covers the orchestration engine
(graph validation, gates, policy verdicts, retries, rollback, re-planning, approvals,
audit-chain integrity, and metric derivation).

The orchestrator also runs the service suite for real, under the stdlib `trace` module, and
gates on measured statement coverage. Approach, limits, and what is deliberately *not*
tested are in [docs/TESTING.md](docs/TESTING.md).

---

## Documents

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, orchestration model, control flow, key decisions |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | Greenfield, brownfield, and ambiguous walkthroughs with real output |
| [docs/TESTING.md](docs/TESTING.md) | Testing strategy, coverage measurement, and its limits |
| [docs/RISKS_AND_TRADEOFFS.md](docs/RISKS_AND_TRADEOFFS.md) | Failure modes, guardrails, and what was consciously given up |
| [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md) | Plan, rationale, artifacts, assumptions, limitations |
| [docs/openapi.yaml](docs/openapi.yaml) | Generated API contract |
