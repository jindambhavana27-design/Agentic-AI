package com.example.orchestrator;

/** Workflow actions backed by reusable project-local Claude Skills. */
public final class ClaudeSkillActions {
    private ClaudeSkillActions() {}

    public static WorkflowNode.NodeAction runSkill(
            ClaudeSkillRunner runner, String skill, String outputKey,
            String fallback, boolean allowEdits, TaskBuilder taskBuilder) {
        return context -> {
            if (!runner.enabled()) {
                context.data().put(outputKey, fallback);
                context.data().put(outputKey + "Mode", "deterministic-fallback");
                return NodeResult.ok(skill + " skipped; Claude Code unavailable, deterministic fallback used");
            }
            String task = taskBuilder.build(context);
            String result = runner.run(skill, task, allowEdits);
            context.data().put(outputKey, result);
            context.data().put(outputKey + "Mode", "claude-skill");
            return NodeResult.ok(skill + " completed by Claude Skill");
        };
    }

    @FunctionalInterface
    public interface TaskBuilder { String build(RunContext context); }
}
