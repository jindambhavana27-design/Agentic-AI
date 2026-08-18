package com.example.shortener.domain;
import java.time.Instant;
import java.util.Map;
public record LinkStats(String code, long clicks, Instant windowStart, Instant windowEnd, Map<String,Long> referrers) {}
