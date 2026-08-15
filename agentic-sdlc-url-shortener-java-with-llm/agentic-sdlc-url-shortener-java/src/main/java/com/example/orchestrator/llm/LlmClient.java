package com.example.orchestrator.llm;

/** Simple provider-neutral contract for reasoning agents. */
public interface LlmClient {
    String complete(String systemPrompt, String userPrompt) throws Exception;

    default boolean enabled() {
        return true;
    }
}
