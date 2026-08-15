package com.example.shortener.api;
import jakarta.validation.constraints.NotBlank;
import java.time.Instant;
import java.util.Map;
public final class ApiDtos {
  private ApiDtos() {}
  public record CreateLinkRequest(@NotBlank String url, String alias, Long ttlSeconds, Map<String,Object> metadata) {}
  public record LinkResponse(String code, String url, String shortUrl, Instant createdAt, Instant expiresAt,
                             boolean customAlias, Map<String,Object> metadata) {}
  public record PageResponse(java.util.List<LinkResponse> items, String nextCursor) {}
  public record ErrorBody(ErrorValue error) { public record ErrorValue(String code,String message,String requestId,Map<String,Object> details){} }
}
