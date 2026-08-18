# LLM Integration

The earlier direct `LlmClient` integration has been replaced by project-local **Claude Skills**. See `CLAUDE_SKILLS_INTEGRATION.md`.

The orchestrator now invokes Claude Code skills from `.claude/skills/` for the full SDLC flow rather than calling an OpenAI-compatible chat-completions client directly.
