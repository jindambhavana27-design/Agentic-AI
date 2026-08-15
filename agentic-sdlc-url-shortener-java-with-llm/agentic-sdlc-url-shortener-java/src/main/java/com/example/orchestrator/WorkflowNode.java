package com.example.orchestrator;
import java.util.*;
public record WorkflowNode(String id,String role,Set<String> dependencies,boolean parallel,boolean approvalRequired,int maxRetries,boolean compensable,NodeAction action) {
  public WorkflowNode {dependencies=Set.copyOf(dependencies);}
  @FunctionalInterface public interface NodeAction {NodeResult execute(RunContext context) throws Exception;}
}
