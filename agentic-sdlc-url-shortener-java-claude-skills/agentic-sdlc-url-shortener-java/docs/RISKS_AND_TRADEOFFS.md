Risks, Trade-offs, and Failure Modes

1. LLM risks

Model endpoint is unavailable or slow

OpenAiCompatibleLlmClient uses JDK HttpClient, a 15-second connection timeout, and a
90-second request timeout. A timeout or non-2xx response causes the workflow node to fail
and enter the orchestrator's retry handling.

Trade-off: a live LLM adds latency and availability risk, but it provides reasoning for
requirements, architecture, and planning.

Hosted API quota is exhausted

A hosted OpenAI-compatible endpoint can return errors such as HTTP 429 when credits/quota
are unavailable.

Mitigation used in the assessment: the same Java client can point to a local Ollama
OpenAI-compatible endpoint.

$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"

LLM output is nondeterministic

The request uses a low temperature (0.2), but model output can still vary.

Current control: the LLM does not own dependency ordering, approvals, policy, status,
or audit. Those remain in Java orchestration code.

Next improvement: parse model output into typed/validated JSON contracts before allowing
later stages to consume it.

LLM returns malformed or empty output

The client rejects an empty choices[0].message.content. However, the current implementation
stores non-empty model output as text and does not fully validate the requested JSON shape.

Next improvement: Jackson DTO/schema validation for requirements, architecture, and
planning responses.

2. Orchestrator risks

Dependency failure

A node runs only when all declared dependencies have SUCCEEDED. Otherwise the dependent
node is marked SKIPPED.

This prevents later stages from running as though a failed prerequisite succeeded.

Retry behavior

Each workflow node declares a maximum retry count. The engine retries failed actions up to
that limit and records retry events in the audit log.

Trade-off: the current engine retries immediately; it does not implement exponential
backoff.

Release without evidence

PolicyEngine prevents the release node from running unless:

testsPassed == true

securityPassed == true

This keeps the release decision in deterministic Java policy code rather than LLM output.

Approval behavior

The release node requires approval. In the CLI run used for this assessment, the engine is
invoked with auto-approval enabled for demonstration.

Production improvement: use an authenticated external approval workflow rather than
automatic/local approval.

Rollback scope

Compensable nodes can be marked ROLLED_BACK when execution fails.

Limitation: the current Java prototype records compensation completion in orchestration
state; it is not a full transactional rollback engine for arbitrary file/database changes.

3. URL-shortener risks

Risk

Current control

Remaining limitation

Unauthorized management API access

X-API-Key validation using constant-time comparison

API keys are simple shared secrets

Missing auth configuration

Application startup fails when auth is required and no keys exist

Key rotation is not implemented

Request abuse

Token-bucket rate limiter

State is local to one JVM

Predictable codes

SecureRandom Base62 generation

Collision retries are still required

Alias collision

Conflict response

No reservation/namespace model

Unsafe URLs

UrlValidator

Production SSRF protection should be reviewed against deployment/network rules

Expired links

Resolve returns 410 Gone

Expired records remain in the in-memory store

Deleted links

Soft delete and 410 Gone

Tombstones remain in memory

Analytics growth

Click events are stored for aggregation

Current in-memory event list is not suitable for high volume

Data loss on restart

None for InMemoryLinkStore

Persistent production storage is required

Horizontal scaling

None for in-memory state/rate limiting

Requires shared/persistent infrastructure

4. Main trade-offs

In-memory storage

Bought: simple local execution with no external database setup.

Paid: links, idempotency data, and analytics disappear when the JVM stops and are not
shared across replicas.

Although the Maven project contains the SQLite JDBC dependency, the current Java runtime
source included in this assessment wires InMemoryLinkStore.

Spring MVC

Bought: familiar production-shaped Java REST API, validation, interceptors, and clear
controller/service separation.

Paid: more framework/runtime dependencies than a pure JDK HTTP server.

Local Ollama

Bought: no hosted-model credit requirement and no need to send assessment prompts to a
remote model provider.

Paid: model startup/inference speed depends on the developer machine.

OpenAI-compatible abstraction

Bought: the same Java client can use multiple compatible model endpoints by changing
environment variables.

Paid: provider-specific features are intentionally not used.

5. What should be improved next

Validate LLM responses with typed Jackson DTOs/schema.

Add retry backoff and clearer timeout/error classification.

Record model name, latency, and request identifiers in safe audit metadata.

Expand JUnit tests for LLM failure modes, API security, expiration, rate limiting,
analytics, and controller behavior.

Replace in-memory storage with a persistent LinkStore implementation for production.

Use shared rate-limit state for multi-instance deployment.

Integrate the test/security workflow nodes with real Maven/security commands if this
prototype is extended into a production SDLC system.