package com.example.orchestrator;

import java.nio.file.Path;

public class OrchestratorCli {
    public static void main(String[] args) {
        String command = args.length > 0 ? args[0] : "plan";
        String scenario = args.length > 1 ? args[1] : "greenfield";
        String userRequirement = args.length > 2 ? String.join(" ", java.util.Arrays.copyOfRange(args, 2, args.length)) : null;
        Path projectRoot = Path.of(".").toAbsolutePath().normalize();
        ClaudeSkillRunner skills = new ClaudeSkillRunner(projectRoot);
        WorkflowGraph graph = WorkflowFactory.create(scenario, projectRoot);

        System.out.println("Claude Skills mode: " + (skills.enabled() ? "ENABLED" : "DISABLED (deterministic fallback)"));
        if (command.equals("plan")) {
            int i = 0;
            for (var level : graph.levels()) {
                System.out.println("level " + i++);
                for (var n : level) System.out.println("  - " + n.id() + " [" + n.role() + "] <- " + n.dependencies());
            }
            return;
        }

        RunContext context = new RunContext(scenario, projectRoot);
        if (userRequirement != null && !userRequirement.isBlank()) context.data().put("userRequirement", userRequirement);
        var report = new OrchestrationEngine(new ApprovalStore()).run(graph, context, true);

        System.out.println("run=" + report.runId());
        report.statuses().forEach((k, v) -> System.out.println(k + ": " + v));
        System.out.println("auditValid=" + report.auditValid());
        System.out.println("requirementsMode=" + context.data().getOrDefault("requirementsMode", "unknown"));
        printOutput("requirements", context); printOutput("impactAnalysis", context);
        printOutput("architecture", context); printOutput("plan", context);
        printOutput("implementation", context); printOutput("testing", context);
        printOutput("security", context); printOutput("documentation", context);
        printOutput("releaseReadiness", context);
    }

    private static void printOutput(String key, RunContext context) {
        Object value = context.data().get(key);
        if (value != null) {
            System.out.println("\n--- " + key.toUpperCase() + " OUTPUT ---");
            System.out.println(value);
        }
    }
}
