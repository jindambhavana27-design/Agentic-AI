Scenarios

1. Greenfield LLM scenario used in the assessment

The main demonstrated requirement is:

Build a secure URL shortener with expiration, analytics and rate limiting

Step 1 — Inspect the plan

java -cp $cp com.example.orchestrator.OrchestratorCli plan greenfield

The Java orchestrator displays the workflow dependency levels:

requirements
    |
architecture
    |
decompose
    |
implement
    |
test + security + docs
    |
release

Step 2 — Configure the local LLM

Ollama was used through its OpenAI-compatible endpoint:

ollama pull llama3.2

$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"

Step 3 — Run the workflow

java -cp $cp com.example.orchestrator.OrchestratorCli run greenfield "Build a secure URL shortener with expiration, analytics and rate limiting"

The LLM-backed stages analyze requirements, architecture, and planning. The orchestrator
continues to own dependencies, retries, policy, approval, status, and audit.

The demonstrated successful run reported:

requirements: SUCCEEDED
architecture: SUCCEEDED
decompose: SUCCEEDED
implement: SUCCEEDED
test: SUCCEEDED
security: SUCCEEDED
docs: SUCCEEDED
release: SUCCEEDED
auditValid=true
requirementsMode=llm

The CLI also prints the LLM outputs for requirements, architecture, and plan.

2. LLM unavailable / deterministic fallback

If LLM_API_KEY or LLM_MODEL is not configured,
OpenAiCompatibleLlmClient.fromEnvironment() returns NoOpLlmClient.

The same workflow can still be created, but the reasoning stages use deterministic fallback
content and the CLI reports:

LLM mode: DISABLED (deterministic fallback)

This keeps the orchestration code independent from mandatory model availability.

3. Hosted API quota failure

During hosted API execution, an HTTP 429/insufficient-quota response can cause the
requirements or another LLM-backed stage to fail.

The model endpoint can be switched to Ollama without changing the workflow code:

$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"

This scenario demonstrates why the LLM client is abstracted from the orchestration engine.

4. Brownfield workflow

WorkflowFactory also supports a brownfield scenario.

It adds an explicit impact-analysis node:

requirements
    |
impact
    |
architecture
    |
decompose
    |
implement
    |
test + security + docs
    |
release

The current impact action records that existing modules, API contracts, storage, and tests
should be analyzed before the change. Requirements, architecture, and planning can still use
the configured LLM.

Run:

java -cp $cp com.example.orchestrator.OrchestratorCli plan brownfield

or:

java -cp $cp com.example.orchestrator.OrchestratorCli run brownfield "Change the existing URL shortener safely"

5. Actual URL-shortener execution

The Agentic SDLC run is not the same as running the URL-shortener application.

After the orchestration workflow, start the actual Spring Boot service:

$env:SHORTENER_API_KEYS="dev-key-1"
java -cp $cp com.example.shortener.ShortenerApplication

Keep that terminal running.

In another PowerShell terminal, create a link:

curl.exe -X POST "http://localhost:8080/api/v1/links" -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" -d '{\"url\":\"https://www.google.com\",\"alias\":\"demo123\"}'

Then resolve it:

curl.exe -i http://localhost:8080/demo123

The expected behavior is an HTTP redirect whose Location points to the original URL.

This demonstrates the complete separation:

LLM + Orchestrator = SDLC reasoning and workflow execution

Spring Boot URL Shortener = actual application runtime