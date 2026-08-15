package com.example.shortener;

import com.example.shortener.config.ShortenerProperties;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties(ShortenerProperties.class)
public class ShortenerApplication implements CommandLineRunner {
  private final ShortenerProperties properties;
  public ShortenerApplication(ShortenerProperties properties) { this.properties = properties; }
  public static void main(String[] args) { SpringApplication.run(ShortenerApplication.class, args); }
  @Override public void run(String... args) {
    if (properties.requireAuth() && properties.apiKeySet().isEmpty())
      throw new IllegalStateException("SHORTENER_API_KEYS is required when authentication is enabled");
  }
}
