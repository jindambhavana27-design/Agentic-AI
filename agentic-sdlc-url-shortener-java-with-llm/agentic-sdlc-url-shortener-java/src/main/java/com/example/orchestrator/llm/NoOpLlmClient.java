package com.example.orchestrator.llm;

/** Used when no external LLM is configured, keeping tests and demos deterministic. */
public final class NoOpLlmClient implements LlmClient {
    @Override
    public String complete(String systemPrompt, String userPrompt) {
        throw new IllegalStateException("LLM is not configured");
    }

    @Override
    public boolean enabled() {
        return false;
    }
}
