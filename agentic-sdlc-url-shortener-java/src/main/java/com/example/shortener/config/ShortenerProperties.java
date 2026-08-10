package com.example.shortener.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import java.util.*;

@ConfigurationProperties("shortener")
public record ShortenerProperties(String baseUrl, String dbPath, String apiKeys, boolean requireAuth,
  int codeLength, int redirectStatus, Long defaultTtlSeconds, boolean allowPrivateHosts,
  boolean rateLimitEnabled, int rateLimitCapacity, double rateLimitRefillPerSecond, boolean analyticsEnabled) {
  public ShortenerProperties {
    baseUrl = baseUrl == null ? "http://127.0.0.1:8080" : baseUrl;
    dbPath = dbPath == null ? "shortener.db" : dbPath;
    apiKeys = apiKeys == null ? "" : apiKeys;
    codeLength = codeLength <= 0 ? 7 : codeLength;
    redirectStatus = redirectStatus == 301 ? 301 : 302;
    rateLimitCapacity = rateLimitCapacity <= 0 ? 60 : rateLimitCapacity;
    rateLimitRefillPerSecond = rateLimitRefillPerSecond <= 0 ? 1.0 : rateLimitRefillPerSecond;
  }
  public Set<String> apiKeySet() {
    Set<String> keys = new HashSet<>();
    Arrays.stream(apiKeys.split(",")).map(String::trim).filter(s -> !s.isEmpty()).forEach(keys::add);
    return Collections.unmodifiableSet(keys);
  }
}
