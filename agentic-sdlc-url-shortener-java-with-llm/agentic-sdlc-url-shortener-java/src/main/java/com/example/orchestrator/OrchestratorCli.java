package com.example.orchestrator;

import com.example.orchestrator.llm.LlmClient;
import com.example.orchestrator.llm.OpenAiCompatibleLlmClient;

import java.nio.file.Path;

public class OrchestratorCli {
    public static void main(String[] args) {
        String command = args.length > 0 ? args[0] : "plan";
        String scenario = args.length > 1 ? args[1] : "greenfield";
        String userRequirement = args.length > 2 ? String.join(" ", java.util.Arrays.copyOfRange(args, 2, args.length)) : null;

        LlmClient llm = OpenAiCompatibleLlmClient.fromEnvironment();
        WorkflowGraph graph = WorkflowFactory.create(scenario, llm);

        if (command.equals("plan")) {
            System.out.println("LLM mode: " + (llm.enabled() ? "ENABLED" : "DISABLED (deterministic fallback)"));
            int i = 0;
            for (var level : graph.levels()) {
                System.out.println("level " + i++);
                for (var n : level) {
                    System.out.println("  - " + n.id() + " [" + n.role() + "] <- " + n.dependencies());
                }
            }
            return;
        }

        RunContext context = new RunContext(scenario, Path.of("."));
        if (userRequirement != null && !userRequirement.isBlank()) {
            context.data().put("userRequirement", userRequirement);
        }

        System.out.println("LLM mode: " + (llm.enabled() ? "ENABLED" : "DISABLED (deterministic fallback)"));
        var report = new OrchestrationEngine(new ApprovalStore()).run(graph, context, true);

        System.out.println("run=" + report.runId());
        report.statuses().forEach((k, v) -> System.out.println(k + ": " + v));
        System.out.println("auditValid=" + report.auditValid());
        System.out.println("requirementsMode=" + context.data().getOrDefault("requirementsMode", "unknown"));

        if (llm.enabled()) {
            printOutput("requirements", context);
            printOutput("architecture", context);
            printOutput("plan", context);
        }
    }

    private static void printOutput(String key, RunContext context) {
        Object value = context.data().get(key);
        if (value != null) {
            System.out.println("\n--- " + key.toUpperCase() + " OUTPUT ---");
            System.out.println(value);
        }
    }
}
