# Claude Skills SDLC Integration

This version replaces the direct `LlmClient` / OpenAI-compatible agent calls with **Claude Code Agent Skills**.

## Why

Claude Skills are project-local reusable capabilities stored under `.claude/skills/<skill-name>/SKILL.md`. The Java orchestrator remains deterministic and invokes the appropriate skill for each SDLC stage. Claude provides reasoning and repository/tool access; the orchestrator still owns ordering, dependencies, retries, parallelism, audit and human approval.

## End-to-end flow

```text
User requirement
  -> OrchestrationEngine
  -> /requirements-analysis
  -> [/impact-analysis for brownfield]
  -> /architecture-design
  -> /implementation-planning
  -> /java-implementation
  -> /testing-validation + /security-review + /documentation-sync
  -> human approval
  -> /release-readiness (no deployment)
```

## Skills

- `requirements-analysis` - ambiguity, functional/NFRs, acceptance criteria
- `impact-analysis` - brownfield blast radius and compatibility
- `architecture-design` - components, flow, security, scaling and trade-offs
- `implementation-planning` - tasks, dependencies, parallel work and validation
- `java-implementation` - edits Java code and adds/updates tests
- `testing-validation` - runs/reviews Maven tests and reports evidence
- `security-review` - security review and PASS/BLOCK recommendation
- `documentation-sync` - updates README/docs to match behavior
- `release-readiness` - GO/NO_GO recommendation; never deploys

## Prerequisites

1. Java 17+
2. Maven
3. Claude Code installed and authenticated
4. Run from the project root so Claude Code discovers `.claude/skills/`

Verify:

```powershell
java -version
mvn -version
claude --version
```

Authenticate Claude Code using its normal login flow, or configure Anthropic credentials supported by Claude Code. Do not commit credentials.

## Build

```powershell
mvn clean test
mvn -DskipTests package
```

## Plan (no skill execution)

```powershell
mvn exec:java "-Dexec.mainClass=com.example.orchestrator.OrchestratorCli" "-Dexec.args=plan greenfield"
```

## Run end-to-end with Skills

```powershell
mvn exec:java "-Dexec.mainClass=com.example.orchestrator.OrchestratorCli" "-Dexec.args=run greenfield Build a secure URL shortener with expiration analytics and rate limiting"
```

Brownfield:

```powershell
mvn exec:java "-Dexec.mainClass=com.example.orchestrator.OrchestratorCli" "-Dexec.args=run brownfield Add a safe maintenance capability without breaking existing APIs"
```

## Configuration

Optional model selection:

```powershell
$env:CLAUDE_MODEL="sonnet"
```

Disable Skills and use deterministic fallbacks (useful for unit tests/offline demo):

```powershell
$env:CLAUDE_SKILLS_ENABLED="false"
```

Re-enable:

```powershell
$env:CLAUDE_SKILLS_ENABLED="true"
```

## Important design point

Skills do not replace orchestration. Skills provide reusable expertise/instructions and Claude's reasoning/tool execution. `OrchestrationEngine` remains responsible for workflow control and governance. The release node still requires the existing approval gate, and the release skill only assesses readiness; it does not deploy.
