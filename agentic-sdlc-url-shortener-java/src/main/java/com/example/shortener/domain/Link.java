package com.example.shortener.domain;

import java.time.Instant;
import java.util.Map;

public record Link(String code, String targetUrl, Instant createdAt, Instant expiresAt, Instant deletedAt,
                   String createdBy, String idempotencyKey, boolean customAlias, Map<String,Object> metadata) {
  public boolean expired() { return expiresAt != null && !expiresAt.isAfter(Instant.now()); }
  public boolean deleted() { return deletedAt != null; }
  public Link withDeletedAt(Instant value) { return new Link(code,targetUrl,createdAt,expiresAt,value,createdBy,idempotencyKey,customAlias,metadata); }
}
