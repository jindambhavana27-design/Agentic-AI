# Agentic SDLC URL Shortener — Java Conversion

This repository is the Java 17 / Spring Boot conversion of the supplied Python project.
It contains:

1. A production-shaped URL-shortener REST API with custom aliases, idempotency, expiry,
   safe redirects, API-key authentication, rate limiting, analytics, health endpoints,
   Prometheus metrics, consistent errors, and soft deletion.
2. A Java agentic-SDLC orchestration module with a dependency graph, parallel levels,
   policy gates, retries, compensating rollback, approvals, workflow scenarios, and a
   tamper-evident audit chain.

## Run

```bash
export SHORTENER_API_KEYS=dev-key-1
mvn spring-boot:run
```

```bash
curl -X POST http://127.0.0.1:8080/api/v1/links \
  -H 'X-API-Key: dev-key-1' -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/a-very-long-address","alias":"demo"}'
```

## Test

```bash
mvn test
```

## Run the orchestrator

```bash
mvn -q -DskipTests package
java -cp target/agentic-sdlc-url-shortener-1.0.0.jar com.example.orchestrator.OrchestratorCli plan greenfield
```

For IDE execution, run `com.example.orchestrator.OrchestratorCli` directly.

## Conversion notes

- Python standard-library HTTP code became Spring MVC controllers/interceptors.
- Python configuration became typed `@ConfigurationProperties`.
- The storage contract became a Java interface; an in-memory thread-safe adapter is included.
- Micrometer/Actuator provides health and Prometheus endpoints.
- The original OpenAPI and architecture documents are retained under `docs/`.
- The orchestrator design is represented natively in Java rather than invoking Python.
