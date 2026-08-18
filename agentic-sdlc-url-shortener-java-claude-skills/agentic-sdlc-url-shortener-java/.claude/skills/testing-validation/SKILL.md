---
name: testing-validation
description: Validate Java implementation changes by reviewing tests and running appropriate Maven tests, then report evidence, failures, and gaps. Use after implementation and before release.
---
# Testing Validation
Inspect changed code and tests. Run the smallest useful Maven test set, expanding when needed. Do not hide failures. Report commands, pass/fail evidence, coverage gaps, regression risks, and final PASS/FAIL recommendation. Do not change production code; test-only fixes are allowed only when clearly justified.
