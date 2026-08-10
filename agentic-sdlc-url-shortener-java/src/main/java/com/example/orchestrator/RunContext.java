package com.example.orchestrator;
import java.nio.file.Path; import java.util.*; import java.util.concurrent.ConcurrentHashMap;
public class RunContext {private final String runId=UUID.randomUUID().toString();private final String scenario;private final Path workspace;private final Map<String,Object> data=new ConcurrentHashMap<>();public RunContext(String scenario,Path workspace){this.scenario=scenario;this.workspace=workspace;}public String runId(){return runId;}public String scenario(){return scenario;}public Path workspace(){return workspace;}public Map<String,Object> data(){return data;}}
