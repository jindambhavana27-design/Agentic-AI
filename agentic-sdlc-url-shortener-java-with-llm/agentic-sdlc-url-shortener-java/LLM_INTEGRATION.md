# LLM Integration Guide

This version keeps the orchestration engine deterministic, but adds real LLM-backed reasoning to the stages that benefit from language understanding:

- Requirements analysis
- Architecture reasoning
- Task planning

Testing, security, policy enforcement, retries, approvals and release remain deterministic.

## Architecture

```text
User Requirement
      |
      v
OrchestrationEngine (deterministic)
      |
      v
Requirements Node ---> LLM
      |
      v
Architecture Node ---> LLM
      |
      v
Planning Node -------> LLM
      |
      v
Implementation (tool/deterministic prototype)
      |
      +------> Testing
      +------> Security
      +------> Documentation
                    |
                    v
             Human Approval
                    |
                    v
                 Release
```

The LLM is intentionally behind the workflow-node boundary. It does not decide release policy, retries, security gates or approval rules.

## Configure an LLM

The client uses an OpenAI-compatible chat-completions HTTP endpoint.

PowerShell:

```powershell
$env:LLM_API_KEY="YOUR_API_KEY"
$env:LLM_MODEL="YOUR_MODEL_NAME"
# Optional. Defaults to OpenAI's chat-completions endpoint.
$env:LLM_BASE_URL="https://api.openai.com/v1/chat/completions"
```

Do not commit API keys to source control.

## Run

Build and test:

```powershell
mvn clean test
mvn -q -DskipTests package
```

Show the workflow:

```powershell
java -cp target/agentic-sdlc-url-shortener-1.0.0.jar com.example.orchestrator.OrchestratorCli plan greenfield
```

Run with a natural-language requirement:

```powershell
java -cp target/agentic-sdlc-url-shortener-1.0.0.jar com.example.orchestrator.OrchestratorCli run greenfield "Build a secure URL shortener with expiration, analytics and rate limiting"
```

When `LLM_API_KEY` and `LLM_MODEL` are present, the CLI prints `LLM mode: ENABLED` and displays the LLM-produced requirements, architecture and plan.

If those variables are absent, the same workflow runs using deterministic fallback actions. This keeps unit tests reproducible and makes the project runnable without external credentials.

## Interview explanation

> I kept the orchestrator deterministic and plugged the LLM into only the reasoning-heavy stages. Requirements, architecture and planning use the LLM because they benefit from natural-language understanding and reasoning. Testing, security, retries, policies and release approval remain deterministic because those stages need reproducible evidence and governance. The agents communicate through RunContext, so the LLM output from one stage becomes structured context for the next stage.
