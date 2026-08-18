package com.example.orchestrator;

import java.nio.file.Path;
import java.util.Set;

public final class WorkflowFactory {
    private WorkflowFactory() {}

    public static WorkflowGraph create(String scenario) {
        return create(scenario, Path.of("."));
    }

    /** Creates an end-to-end SDLC workflow backed by project-local Claude Skills. */
    public static WorkflowGraph create(String scenario, Path workingDirectory) {
        WorkflowGraph g = new WorkflowGraph();
        ClaudeSkillRunner skills = new ClaudeSkillRunner(workingDirectory);

        g.add(n("requirements", "requirements", Set.of(), false, 1, false,
                ClaudeSkillActions.runSkill(skills, "requirements-analysis", "requirements",
                        scenario, false,
                        c -> "Analyze this requirement for scenario " + scenario + ": " +
                                c.data().getOrDefault("userRequirement", defaultRequirement(scenario)))));

        if ("brownfield".equalsIgnoreCase(scenario)) {
            g.add(n("impact", "impact-analysis", Set.of("requirements"), false, 0, false,
                    ClaudeSkillActions.runSkill(skills, "impact-analysis", "impactAnalysis",
                            "Analyze existing modules, API contracts, storage and tests before change", false,
                            c -> "Perform brownfield impact analysis. Validated requirements:\n" + c.data().get("requirements"))));
        }

        Set<String> architectureDependencies = "brownfield".equalsIgnoreCase(scenario)
                ? Set.of("requirements", "impact") : Set.of("requirements");

        g.add(n("architecture", "architecture", architectureDependencies, false, 1, false,
                ClaudeSkillActions.runSkill(skills, "architecture-design", "architecture",
                        "Spring Boot layered architecture", false,
                        c -> "Design the architecture. Requirements:\n" + c.data().get("requirements") +
                                "\nImpact analysis:\n" + c.data().getOrDefault("impactAnalysis", "not applicable"))));

        g.add(n("decompose", "planning", Set.of("requirements", "architecture"), false, 1, false,
                ClaudeSkillActions.runSkill(skills, "implementation-planning", "plan",
                        "implement service, tests, security and docs", false,
                        c -> "Create an implementation plan. Requirements:\n" + c.data().get("requirements") +
                                "\nArchitecture:\n" + c.data().get("architecture"))));

        g.add(n("implement", "implementation", Set.of("decompose"), false, 1, true,
                ClaudeSkillActions.runSkill(skills, "java-implementation", "implementation",
                        "Java implementation stage completed", true,
                        c -> "Implement the approved plan in this repository. Plan:\n" + c.data().get("plan") +
                                "\nArchitecture:\n" + c.data().get("architecture"))));

        g.add(n("test", "testing", Set.of("implement"), false, 2, true,
                ClaudeSkillActions.runSkill(skills, "testing-validation", "testing",
                        "tests passed", false,
                        c -> "Validate the current implementation. Implementation summary:\n" + c.data().get("implementation"))));

        g.add(n("security", "security", Set.of("implement"), false, 1, false,
                ClaudeSkillActions.runSkill(skills, "security-review", "security",
                        "security review passed", false,
                        c -> "Perform a security review of the current implementation. Implementation summary:\n" + c.data().get("implementation"))));

        g.add(n("docs", "documentation", Set.of("implement"), false, 0, false,
                ClaudeSkillActions.runSkill(skills, "documentation-sync", "documentation",
                        "documentation synchronized", true,
                        c -> "Synchronize project documentation with the implemented change. Implementation summary:\n" + c.data().get("implementation"))));

        g.add(n("release", "release", Set.of("test", "security", "docs"), true, 0, false,
                ClaudeSkillActions.runSkill(skills, "release-readiness", "releaseReadiness",
                        "release readiness checked", false,
                        c -> "Assess release readiness. Testing:\n" + c.data().get("testing") +
                                "\nSecurity:\n" + c.data().get("security") +
                                "\nDocumentation:\n" + c.data().get("documentation"))));
        return g;
    }

    private static WorkflowNode n(String id, String role, Set<String> dependencies, boolean approval,
                                  int retries, boolean compensable, WorkflowNode.NodeAction action) {
        return new WorkflowNode(id, role, dependencies, true, approval, retries, compensable, action);
    }

    private static String defaultRequirement(String scenario) {
        return switch (scenario.toLowerCase()) {
            case "brownfield" -> "Enhance the existing URL shortener with a safe maintenance capability while preserving backward compatibility.";
            case "ambiguous" -> "Make the URL shortener faster and more scalable.";
            default -> "Build a production-style URL shortener with create, redirect, expiration, authentication, rate limiting, analytics and observability.";
        };
    }
}
