"""SDLC agents.

Each agent owns one lifecycle concern and communicates only through the shared
context. They perform real work against the real workspace -- parsing source,
running the test suite, scanning for vulnerabilities, diffing documentation
against the implementation -- so the signals the engine gates on are measured,
not asserted.
"""

from .architect import ArchitectAgent
from .base import Agent, AgentContext, CallableAgent, FlakyAgentWrapper
from .docs import DocumentationAgent
from .impact import ImpactAnalysisAgent
from .implementer import ChangePlan, FileChange, ImplementationAgent
from .planner import PlannerAgent, Task
from .release import ReleaseAgent
from .requirements import RequirementsAgent
from .security import SecurityAgent
from .tester import TestAgent

__all__ = [
    "Agent", "AgentContext", "CallableAgent", "FlakyAgentWrapper",
    "RequirementsAgent", "ArchitectAgent", "PlannerAgent", "Task",
    "ImpactAnalysisAgent", "ImplementationAgent", "ChangePlan", "FileChange",
    "TestAgent", "SecurityAgent", "DocumentationAgent", "ReleaseAgent",
]
