package com.example.orchestrator;
import java.util.*;
public class PolicyEngine {public Verdict evaluate(WorkflowNode n,RunContext c){List<String> reasons=new ArrayList<>();if(n.role().equals("release")&&!Boolean.TRUE.equals(c.data().get("testsPassed")))reasons.add("release requires passing tests");if(n.role().equals("release")&&!Boolean.TRUE.equals(c.data().get("securityPassed")))reasons.add("release requires security approval");return new Verdict(reasons.isEmpty(),reasons);}public record Verdict(boolean allowed,List<String> reasons){}}
