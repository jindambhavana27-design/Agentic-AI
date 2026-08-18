package com.example.shortener.domain;
import java.time.Instant;
public record ClickEvent(String code, Instant timestamp, String referrer, String userAgent) {}
