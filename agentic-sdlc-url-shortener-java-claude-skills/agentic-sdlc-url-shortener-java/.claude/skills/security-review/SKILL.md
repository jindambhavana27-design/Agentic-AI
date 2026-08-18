---
name: security-review
description: Review Java/Spring Boot changes for authentication, authorization, input validation, SSRF, injection, secrets, logging, rate limiting, dependency, and data-exposure risks. Use after implementation and before release.
---
# Security Review
Review relevant changes and configuration. Check authn/authz, validation, injection, SSRF/open redirect, secrets, sensitive logging, rate limiting, error leakage, dependency/config risks, and unsafe defaults. Do not weaken controls. Return findings by severity and a PASS/BLOCK recommendation.
