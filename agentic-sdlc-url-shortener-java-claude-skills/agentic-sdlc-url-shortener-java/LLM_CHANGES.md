# Changes from direct LLM agents to Claude Skills

- Removed `LlmClient`, `NoOpLlmClient`, `OpenAiCompatibleLlmClient`, and `LlmReasoningActions`.
- Added `ClaudeSkillRunner` to invoke Claude Code non-interactively from Java.
- Added `ClaudeSkillActions` as the workflow-node adapter.
- Added nine reusable skills under `.claude/skills/`.
- Extended skill-backed execution from requirements/architecture/planning through implementation, testing, security, documentation, and release readiness.
- Preserved deterministic orchestration, dependencies, retries, parallel branches, audit, and human release approval.
- Added deterministic fallback when Claude Code is unavailable or `CLAUDE_SKILLS_ENABLED=false`.
