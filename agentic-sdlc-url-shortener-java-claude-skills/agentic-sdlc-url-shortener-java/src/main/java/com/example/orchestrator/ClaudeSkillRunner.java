package com.example.orchestrator;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/** Runs project-local Claude Code skills from .claude/skills using the Claude CLI. */
public final class ClaudeSkillRunner {
    private final Path workingDirectory;
    private final String model;

    public ClaudeSkillRunner(Path workingDirectory) {
        this.workingDirectory = workingDirectory.toAbsolutePath().normalize();
        this.model = System.getenv().getOrDefault("CLAUDE_MODEL", "sonnet");
    }

    public boolean enabled() {
        String configured = System.getenv().getOrDefault("CLAUDE_SKILLS_ENABLED", "true");
        if (!Boolean.parseBoolean(configured)) return false;
        try {
            Process p = new ProcessBuilder(commandName(), "--version")
                    .redirectErrorStream(true).start();
            return p.waitFor(10, TimeUnit.SECONDS) && p.exitValue() == 0;
        } catch (Exception e) {
            return false;
        }
    }

    public String run(String skillName, String task, boolean allowEdits) throws IOException, InterruptedException {
        List<String> command = new ArrayList<>();
        command.add(commandName());
        command.add("-p");
        command.add("--model");
        command.add(model);
        command.add("--output-format");
        command.add("text");
        command.add("--max-turns");
        command.add(allowEdits ? "12" : "6");
        if (allowEdits) {
            command.add("--permission-mode");
            command.add("acceptEdits");
            command.add("--allowedTools");
            command.add("Read,Edit,Write,Bash,Glob,Grep");
        } else {
            command.add("--permission-mode");
            command.add("plan");
            command.add("--allowedTools");
            command.add("Read,Glob,Grep,Bash");
        }
        command.add("Use the /" + skillName + " skill for this task. " + task);

        Process process = new ProcessBuilder(command)
                .directory(workingDirectory.toFile())
                .redirectErrorStream(true)
                .start();

        StringBuilder out = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) out.append(line).append(System.lineSeparator());
        }
        if (!process.waitFor(10, TimeUnit.MINUTES)) {
            process.destroyForcibly();
            throw new IOException("Claude skill timed out: " + skillName);
        }
        if (process.exitValue() != 0) {
            throw new IOException("Claude skill failed (exit=" + process.exitValue() + "): " + out);
        }
        return out.toString().trim();
    }

    private static String commandName() {
        return System.getProperty("os.name", "").toLowerCase().contains("win") ? "claude.exe" : "claude";
    }
}
