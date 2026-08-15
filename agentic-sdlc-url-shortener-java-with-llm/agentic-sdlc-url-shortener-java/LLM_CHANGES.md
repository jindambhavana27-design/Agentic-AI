# What Changed

This version adds an optional real LLM reasoning layer without changing the deterministic orchestration controls.

## Added

- `orchestrator/llm/LlmClient.java` - provider-neutral LLM contract.
- `orchestrator/llm/OpenAiCompatibleLlmClient.java` - real HTTP client for an OpenAI-compatible model endpoint.
- `orchestrator/llm/NoOpLlmClient.java` - deterministic fallback used when no model is configured.
- `LlmReasoningActions.java` - LLM-backed Requirements, Architecture and Planning actions.
- `WorkflowFactory.create(scenario, llm)` - injects an LLM into reasoning-heavy workflow nodes.
- CLI support for passing a natural-language requirement.
- `LLM_INTEGRATION.md` with setup and interview explanation.

## Kept deterministic on purpose

- Orchestration and dependency scheduling
- Testing
- Security checks
- Policy enforcement
- Retry/rollback
- Human approval
- Release

This split is intentional: probabilistic reasoning is used where it adds value, while governance and verification remain reproducible.
