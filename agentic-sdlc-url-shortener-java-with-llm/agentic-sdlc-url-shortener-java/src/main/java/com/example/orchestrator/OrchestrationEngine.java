package com.example.orchestrator;
import java.util.*; import java.util.concurrent.*;
public class OrchestrationEngine {private final PolicyEngine policy=new PolicyEngine();
    private final AuditLog audit=new AuditLog();
    private final ApprovalStore approvals;
    public OrchestrationEngine(ApprovalStore a){this.approvals=a;}
    public RunReport run(WorkflowGraph graph,RunContext context,boolean autoApprove)
    {graph.validate();Map<String,NodeStatus> statuses=new LinkedHashMap<>();graph.nodes().forEach(n->statuses.put(n.id(),NodeStatus.PENDING));ExecutorService pool=Executors.newFixedThreadPool(4);
        try{for(List<WorkflowNode> level:graph.levels()){List<Callable<Void>> tasks=new ArrayList<>();for(WorkflowNode n:level)tasks.add(()->{execute(n,context,statuses,autoApprove);return null;});
    for(Future<Void> f:pool.invokeAll(tasks))f.get();if(level.stream().anyMatch(n->statuses.get(n.id())==NodeStatus.FAILED))break;}}
catch(Exception e)
{e.printStackTrace();
    throw new RuntimeException(e);}finally{pool.shutdown();}return new RunReport(context.runId(),Map.copyOf(statuses),audit.verify());}private synchronized void execute(WorkflowNode n,RunContext c,Map<String,NodeStatus>s,boolean auto){if(n.dependencies().stream().anyMatch(d->s.get(d)!=NodeStatus.SUCCEEDED)){s.put(n.id(),NodeStatus.SKIPPED);return;}var v=policy.evaluate(n,c);if(!v.allowed()){s.put(n.id(),NodeStatus.FAILED);audit.append(c.runId(),n.id(),"policy_denied",String.join(";",v.reasons()));return;}if(n.approvalRequired()){if(auto)approvals.decide(c.runId(),n.id(),ApprovalStore.Decision.APPROVED);var d=approvals.get(c.runId(),n.id());if(d!=ApprovalStore.Decision.APPROVED){s.put(n.id(),d==ApprovalStore.Decision.REJECTED?NodeStatus.FAILED:NodeStatus.WAITING_APPROVAL);return;}}s.put(n.id(),NodeStatus.RUNNING);audit.append(c.runId(),n.id(),"started",n.role());Exception last=null;for(int attempt=0;attempt<=n.maxRetries();attempt++){try{NodeResult r=n.action().execute(c);if(r.success()){s.put(n.id(),NodeStatus.SUCCEEDED);audit.append(c.runId(),n.id(),"succeeded",r.summary());return;}last=new IllegalStateException(r.summary());}
catch(Exception e)
{
    e.printStackTrace();last=e;}audit.append(c.runId(),n.id(),"retry","attempt="+(attempt+1));}s.put(n.id(),NodeStatus.FAILED);audit.append(c.runId(),n.id(),"failed",String.valueOf(last));if(n.compensable()){s.put(n.id(),NodeStatus.ROLLED_BACK);audit.append(c.runId(),n.id(),"rolled_back","compensation complete");}}public record RunReport(String runId,Map<String,NodeStatus> statuses,boolean auditValid){}}
