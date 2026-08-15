package com.example.orchestrator;

import com.example.orchestrator.llm.LlmClient;
import com.example.orchestrator.llm.NoOpLlmClient;

import java.util.Set;

public final class WorkflowFactory {
    private WorkflowFactory() {}

    /** Keeps existing tests/demos deterministic when no LLM is supplied. */
    public static WorkflowGraph create(String scenario) {
        return create(scenario, new NoOpLlmClient());
    }

    /** Creates a workflow where reasoning-heavy stages can be backed by an LLM. */
    public static WorkflowGraph create(String scenario, LlmClient llm) {
        WorkflowGraph g = new WorkflowGraph();

        g.add(n(
                "requirements",
                "requirements",
                Set.of(),
                false,
                1,
                false,
                LlmReasoningActions.requirements(llm, scenario)
        ));

        // Brownfield adds an explicit impact-analysis step before architecture.
        if ("brownfield".equalsIgnoreCase(scenario)) {
            g.add(n(
                    "impact",
                    "impact-analysis",
                    Set.of("requirements"),
                    false,
                    0,
                    false,
                    c -> {
                        c.data().put("impactAnalysis",
                                "Analyze existing modules, API contracts, storage and tests before change");
                        return NodeResult.ok("brownfield impact analysis completed");
                    }
            ));
        }

        Set<String> architectureDependencies = "brownfield".equalsIgnoreCase(scenario)
                ? Set.of("requirements", "impact")
                : Set.of("requirements");

        g.add(n(
                "architecture",
                "architecture",
                architectureDependencies,
                false,
                1,
                false,
                LlmReasoningActions.architecture(llm)
        ));

        g.add(n(
                "decompose",
                "planning",
                Set.of("requirements", "architecture"),
                false,
                1,
                false,
                LlmReasoningActions.planning(llm)
        ));

        // Implementation remains deterministic in this prototype; an LLM code agent can be added here later.
        g.add(n(
                "implement",
                "implementation",
                Set.of("decompose"),
                false,
                1,
                true,
                c -> {
                    c.data().put("implementationMode", "deterministic/tool-driven");
                    return NodeResult.ok("Java implementation stage completed");
                }
        ));

        // Validation/governance stages intentionally remain deterministic.
        g.add(n(
                "test",
                "testing",
                Set.of("implement"),
                false,
                2,
                true,
                c -> {
                    c.data().put("testsPassed", true);
                    return NodeResult.ok("tests passed");
                }
        ));

        g.add(n(
                "security",
                "security",
                Set.of("implement"),
                false,
                1,
                false,
                c -> {
                    c.data().put("securityPassed", true);
                    return NodeResult.ok("security review passed");
                }
        ));

        g.add(n(
                "docs",
                "documentation",
                Set.of("implement"),
                false,
                0,
                false,
                c -> NodeResult.ok("documentation synchronized")
        ));

        g.add(n(
                "release",
                "release",
                Set.of("test", "security", "docs"),
                true,
                0,
                false,
                c -> NodeResult.ok("release approved")
        ));

        return g;
    }

    private static WorkflowNode n(
            String id,
            String role,
            Set<String> dependencies,
            boolean approval,
            int retries,
            boolean compensable,
            WorkflowNode.NodeAction action
    ) {
        return new WorkflowNode(id, role, dependencies, true, approval, retries, compensable, action);
    }
}
