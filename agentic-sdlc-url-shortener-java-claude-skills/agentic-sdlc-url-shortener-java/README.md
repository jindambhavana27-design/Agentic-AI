> **Claude Skills edition:** This project now uses project-local Claude Code Skills under `.claude/skills/` for end-to-end SDLC reasoning and execution. Direct `LlmClient` agent calls were removed. See `CLAUDE_SKILLS_INTEGRATION.md` for setup and execution.

# Agentic SDLC URL Shortener — Java

This repository contains:
1. An Agentic SDLC orchestration workflow using an LLM.
2. A Spring Boot URL Shortener application.

## 1. Build the Project

```powershell
mvn clean package -DskipTests
mvn dependency:build-classpath "-Dmdep.outputFile=classpath.txt"
$cp = Get-Content classpath.txt
$cp = "target\\classes;$cp"
```


## 2. Configure the LLM

### Ollama (Local LLM)

```powershell
ollama pull llama3.2
ollama run llama3.2 "Say hello in one sentence"
```

```powershell
$env:LLM_API_KEY="ollama"
$env:LLM_MODEL="llama3.2"
$env:LLM_BASE_URL="http://localhost:11434/v1/chat/completions"
```

### OpenAI (Optional)

```powershell
$env:LLM_API_KEY="YOUR_API_KEY"
$env:LLM_MODEL="gpt-4.1-mini"
$env:LLM_BASE_URL="https://api.openai.com/v1/chat/completions"
```

## 3. View the Agentic SDLC Plan

```powershell
java -cp $cp com.example.orchestrator.OrchestratorCli plan greenfield
```

```text
requirements
    ↓
architecture
    ↓
decompose / planning
    ↓
implement
    ↓
test + security + docs
    ↓
release
```

## 4. Execute the Agentic SDLC Workflow

```powershell
java -cp $cp com.example.orchestrator.OrchestratorCli run greenfield "Build a secure URL shortener with expiration, analytics and rate limiting"
```

A successful run should show:

```text
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
```

`requirementsMode=llm` confirms that the LLM was used for the requirements stage.

## 5. Run the Actual URL Shortener

```powershell
$env:SHORTENER_API_KEYS="dev-key-1"
```

Start the Spring Boot application:

```powershell
java -cp $cp com.example.shortener.ShortenerApplication
```

Keep this terminal running. The application starts on port `8080`.

## 6. Create a Short URL

Open a second PowerShell terminal:

```powershell
curl.exe -X POST "http://localhost:8080/api/v1/links" -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" -d '{\"url\":\"https://www.google.com\",\"alias\":\"demo12345\"}'
```

This creates:

```text
demo12345 → https://www.google.com
```

## 7. Test the Redirect

```powershell
curl.exe -i http://127.0.0.1:8080/demo12345
```

The response should redirect to:

```text
https://www.google.com
```

You can also open:

```text
http://localhost:8080/demo123
```

## Overall Flow

```text
Agentic SDLC:
Requirement → Orchestrator → LLM Requirements → Architecture → Planning
→ Implementation → Testing/Security/Documentation → Release

URL Shortener:
POST /api/v1/links → Create short URL → Store mapping
→ GET /demo123 → Redirect to original URL
```
