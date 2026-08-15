package com.example.orchestrator.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Minimal OpenAI-compatible chat client using JDK HttpClient.
 * Configure with LLM_API_KEY, LLM_MODEL and optionally LLM_BASE_URL.
 */
public final class OpenAiCompatibleLlmClient implements LlmClient {
    private final HttpClient http;
    private final ObjectMapper mapper;
    private final String endpoint;
    private final String apiKey;
    private final String model;

    public OpenAiCompatibleLlmClient(String endpoint, String apiKey, String model) {
        this.endpoint = endpoint;
        this.apiKey = apiKey;
        this.model = model;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMinutes(15))
                .build();
        this.mapper = new ObjectMapper();
    }

    public static LlmClient fromEnvironment() {
        String key = System.getenv("LLM_API_KEY");
        String model = System.getenv("LLM_MODEL");
        if (key == null || key.isBlank() || model == null || model.isBlank()) {
            return new NoOpLlmClient();
        }
        String base = System.getenv().getOrDefault(
                "LLM_BASE_URL",
                "https://api.openai.com/v1/chat/completions"
        );
        return new OpenAiCompatibleLlmClient(base, key, model);
    }

    @Override
    public String complete(String systemPrompt, String userPrompt) throws Exception {
        Map<String, Object> payload = Map.of(
                "model", model,
                "temperature", 0.2,
                "messages", List.of(
                        Map.of("role", "system", "content", systemPrompt),
                        Map.of("role", "user", "content", userPrompt)
                )
        );

        HttpRequest request = HttpRequest.newBuilder(URI.create(endpoint))
                .timeout(Duration.ofMinutes(90))
                .header("Authorization", "Bearer " + apiKey)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(payload)))
                .build();

      HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());

System.out.println("LLM HTTP status: " + response.statusCode());
System.out.println("LLM response: " + response.body());

if (response.statusCode() < 200 || response.statusCode() >= 300) {
    throw new IllegalStateException(
            "LLM request failed: HTTP " + response.statusCode() + " - " + response.body()
    );
}

        JsonNode root = mapper.readTree(response.body());
        JsonNode content = root.path("choices").path(0).path("message").path("content");
        if (content.isMissingNode() || content.asText().isBlank()) {
            throw new IllegalStateException("LLM returned an empty response");
        }
        return content.asText();
    }
}
