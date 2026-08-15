Final Engineering Summary

1. What was built

This assessment is a Java 17 / Spring Boot implementation with two parts:

A Java Agentic SDLC orchestrator.

A secure URL-shortener REST application.

The orchestrator accepts a requirement, builds a dependency graph, executes SDLC stages,
tracks status, applies policy/approval controls, records audit events, and uses an
OpenAI-compatible LLM for reasoning-heavy stages.

The URL shortener is the actual Spring Boot application and runs independently from the
orchestrator.

2. Technology

Java 17

Spring Boot 3.3.2

Spring MVC

Jakarta Validation

Jackson

JDK HttpClient

JUnit 5 / Spring Boot Test

Micrometer + Prometheus

Maven

OpenAI-compatible LLM API

Ollama for local LLM execution

The Maven project also includes the SQLite JDBC dependency, although the current runtime
store implementation in the Java source is InMemoryLinkStore.

3. Agentic SDLC workflow

The greenfield workflow is:

requirements
    |
architecture
    |
decompose
    |
implement
    |
+---------+----------+
|         |          |
test   security     docs
|         |          |
+---------+----------+
          |
       release

The orchestrator is responsible for execution and governance. The LLM is responsible only
for reasoning/output in stages that explicitly invoke it.

LLM-backed stages

requirements

architecture

decompose / planning

Deterministic stages in the current prototype

implement

test

security

docs

release

This distinction is important: the current implementation stage reports a
deterministic/tool-driven implementation stage; it is not an LLM code-generation agent.

4. LLM integration

The Java classes involved are:

OrchestratorCli
    -> WorkflowFactory
    -> LlmReasoningActions
    -> LlmClient
    -> OpenAiCompatibleLlmClient
    -> Ollama / OpenAI-compatible endpoint

Configuration:

$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"

The local Ollama option was used to avoid dependency on hosted API credits while keeping the
same OpenAI-compatible Java client.

During the demonstrated run, the workflow completed all stages and printed:

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

requirementsMode=llm confirms that the requirements stage used the configured LLM.

5. URL-shortener functionality

The Spring Boot service supports:

Create short links.

Custom aliases.

Generated random short codes.

Expiration/TTL.

API-key authentication.

Rate limiting.

Idempotency keys.

Public redirect.

Click analytics.

Link statistics.

Link listing.

Soft deletion.

Health/metrics through Spring Boot Actuator/Micrometer.

The service starts only when required authentication is configured:

$env:SHORTENER_API_KEYS="dev-key-1"
java -cp $cp com.example.shortener.ShortenerApplication

6. Demonstrated service execution

Create a link:

curl.exe -X POST "http://localhost:8080/api/v1/links" -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" -d '{\"url\":\"https://www.google.com\",\"alias\":\"demo123\"}'

Test the redirect:

curl.exe -i http://localhost:8080/demo123

The redirect verifies that the actual URL-shortener application runs separately from the
Agentic SDLC orchestration.

7. Current automated tests

The repository contains JUnit tests for:

OrchestrationEngine workflow completion and audit validity.

ShortenerService alias creation/resolution.

ShortenerService idempotency replay.

The latest packaged test reports in the supplied Java project show:

OrchestrationEngineTest: 1 test, 0 failures
ShortenerServiceTest:    2 tests, 0 failures

Run all tests with:

mvn test

8. Design decisions

Decision

Reason

Java 17 + Spring Boot

Matches the requested Java backend implementation

Dependency graph

Makes workflow dependencies explicit

Parallel execution by graph level

Allows independent stages such as test/security/docs to run together

LLM abstraction

Keeps orchestration independent of a specific model provider

OpenAI-compatible client

Supports hosted compatible APIs and local Ollama

Deterministic fallback

Allows the workflow to run when LLM configuration is absent

API-key interceptor

Keeps management endpoints protected

Token-bucket limiter

Provides simple request throttling

SecureRandom Base62 codes

Avoids predictable sequential short codes

In-memory store

Keeps the assessment simple and executable without external infrastructure

9. Current limitations

LLM output is stored as text rather than mapped into strongly typed response objects.

The LLM-backed stages depend on an external/local model endpoint and can fail or time out.

Implementation is currently deterministic/tool-driven rather than LLM-generated code.

Test and security workflow nodes currently set validation results in the orchestration
context; they do not launch a complete external CI/security toolchain.

InMemoryLinkStore is not durable and is not suitable for multi-instance production use.

Rate limiting is process-local.

The current JUnit suite is intentionally small and should be expanded for production.

The OpenAPI document describes the URL-shortener HTTP API; the orchestrator is a CLI and
therefore is not part of that HTTP contract.

10. Defensible assessment claim

The project demonstrates a Java-based Agentic SDLC orchestration layer that coordinates an
explicit dependency graph and integrates an LLM for requirements, architecture, and
planning while retaining workflow control in Java. It also demonstrates a separately
executable Spring Boot URL-shortener service with authentication, rate limiting, expiration,
analytics, idempotency, and redirects.