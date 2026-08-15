Architecture

1. Overview

The assessment is implemented entirely in Java 17 and contains two runtime concerns:

Agentic SDLC Orchestrator — coordinates the software-delivery workflow.

Spring Boot URL Shortener — the actual application being built and executed.

The orchestrator controls workflow order, dependencies, retries, approvals, policy checks,
status, and audit. Reasoning-heavy stages can call an LLM through an OpenAI-compatible
client. The URL shortener is a separate Spring Boot application.

User requirement
      |
      v
OrchestratorCli
      |
      v
WorkflowFactory --> WorkflowGraph
      |
      v
OrchestrationEngine
      |
      +--> requirements ----+
      |                     |
      +--> architecture ----+--> LlmReasoningActions
      |                     |          |
      +--> decompose -------+          v
      |                         OpenAiCompatibleLlmClient
      |                                  |
      |                         OpenAI-compatible API
      |                         or local Ollama
      |
      +--> implement
      |
      +--> test | security | docs
      |
      v
    release

The Spring Boot application is started separately:

ShortenerApplication
      |
      v
Spring MVC / LinkController
      |
      v
ShortenerService
      |
      +--> UrlValidator
      +--> LinkStore / InMemoryLinkStore
      +--> TokenBucketRateLimiter

2. Orchestrator components

Java class

Responsibility

OrchestratorCli

CLI entry point for plan and run

WorkflowFactory

Builds greenfield/brownfield workflow nodes and dependencies

WorkflowGraph

Validates dependencies/cycles and calculates executable levels

OrchestrationEngine

Executes workflow levels, parallel work, retries, approvals, rollback status and audit

RunContext

Shared data for a workflow run

PolicyEngine

Applies release policy checks

ApprovalStore

Stores approval decisions

AuditLog

Records workflow events and verifies the audit chain

LlmReasoningActions

Builds LLM-backed requirements, architecture and planning actions

LlmClient

Abstraction for model calls

OpenAiCompatibleLlmClient

JDK HttpClient implementation for OpenAI-compatible endpoints

NoOpLlmClient

Deterministic fallback when LLM configuration is absent

3. Workflow

For the greenfield scenario the graph is:

level 0
  requirements

level 1
  architecture <- requirements

level 2
  decompose <- requirements, architecture

level 3
  implement <- decompose

level 4
  test     <- implement
  security <- implement
  docs     <- implement

level 5
  release <- test, security, docs

test, security, and docs are independent after implementation and can execute in the
same level. release waits until all three dependencies complete.

The brownfield workflow additionally places impact between requirements and architecture.

4. LLM integration

The LLM is used for the reasoning-heavy stages currently implemented in
LlmReasoningActions:

Requirements analysis

Architecture reasoning

Planning/decomposition

The implementation, testing, security, documentation, and release actions currently remain
deterministic/tool-driven in this prototype.

The call path is:

OrchestratorCli
  -> OpenAiCompatibleLlmClient.fromEnvironment()
  -> WorkflowFactory.create(scenario, llm)
  -> LlmReasoningActions
  -> LlmClient.complete(systemPrompt, userPrompt)
  -> OpenAI-compatible HTTP endpoint

Configuration is external:

$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"

Because Ollama exposes an OpenAI-compatible endpoint, the same Java client can call a local
model without changing orchestration code.

If LLM_API_KEY or LLM_MODEL is missing, OpenAiCompatibleLlmClient.fromEnvironment()
returns NoOpLlmClient, and the workflow uses deterministic fallback actions.

5. LLM responsibilities vs orchestrator responsibilities

The LLM does not control the workflow.

The LLM produces reasoning content for the stages that call it. The orchestrator still
controls:

Which node runs.

Dependency ordering.

Parallel execution levels.

Retry attempts.

Approval handling.

Policy evaluation.

Node status.

Audit events.

Release dependency checks.

This separation prevents model output from directly bypassing orchestration controls.

6. URL-shortener architecture

The application uses Spring Boot 3.3.2 and Spring MVC.

HTTP request
    |
    v
ApiKeyInterceptor
    |-- API-key validation
    |-- token-bucket rate limiting
    v
LinkController
    v
ShortenerService
    |-- URL validation
    |-- alias/random code generation
    |-- TTL/expiration
    |-- idempotency
    |-- soft delete
    |-- analytics
    v
LinkStore
    v
InMemoryLinkStore

Management API endpoints under /api/v1/** pass through ApiKeyInterceptor. Redirects are
public. The application refuses to start when authentication is required but
SHORTENER_API_KEYS is empty.

7. Main service capabilities

The Java service currently implements:

Custom aliases and generated Base62 short codes.

URL validation.

Optional TTL/expiration.

API-key authentication for management endpoints.

Token-bucket rate limiting.

Idempotency-key replay protection.

Soft deletion.

Click analytics and referrer aggregation.

Link listing and metadata retrieval.

Redirects using configurable 301 or 302.

Spring Boot Actuator and Prometheus support.

8. Execution

Build and create the runtime classpath:

mvn clean package -DskipTests
mvn dependency:build-classpath "-Dmdep.outputFile=classpath.txt"
$cp = Get-Content classpath.txt
$cp = "target\classes;$cp"

Inspect the workflow:

java -cp $cp com.example.orchestrator.OrchestratorCli plan greenfield

Execute with the LLM:

java -cp $cp com.example.orchestrator.OrchestratorCli run greenfield "Build a secure URL shortener with expiration, analytics and rate limiting"

Run the actual service:

$env:SHORTENER_API_KEYS="dev-key-1"
java -cp $cp com.example.shortener.ShortenerApplication