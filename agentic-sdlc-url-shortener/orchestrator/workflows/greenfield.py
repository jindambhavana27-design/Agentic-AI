"""Scenario 1 -- Greenfield: build a new capability from nothing.

Requirement: link tagging and tag-based search. No code for it exists.

What this scenario is meant to demonstrate:

* a well-specified requirement passing the ambiguity gate without escalation;
* **runtime graph expansion** -- the planner decomposes the work and injects one
  specification node per task into the *running* graph, then rewires the
  implementation node to depend on all of them, creating a real synchronisation
  barrier that did not exist when the run started;
* **parallel verification** -- test, security, and documentation run
  concurrently and rejoin at the release barrier;
* a **human approval checkpoint** on release that no green checklist can bypass.

The code the run produces is real: a module and a test file written into the
workspace, then executed by the test agent as part of the full suite.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..agents import ArchitectAgent, ChangePlan, FileChange, ImplementationAgent, PlannerAgent
from ..agents.requirements import RequirementsAgent
from ..agents.taskspec import TaskSpecAgent
from ..gates import (
    AgentSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoOpenQuestions,
    ProducedOutputs,
)
from ..graph import Node, WorkflowGraph
from ..types import AutonomyLevel, FailureAction, RetryPolicy, Stage
from .common import verification_tail

REQUIREMENT = """
- Users must be able to attach up to 8 short tags to a link when they create it.
- A tag must be 2 to 24 characters of lowercase letters, digits, or hyphens, and must be normalised before storage.
- The system must reject a link creation request whose tags are invalid, with a 400 response naming the offending tag.
- Users must be able to search their links by tag and by tag prefix, returning links ordered by creation time descending.
- Tag search must return results in under 50 ms p99 for 10000 links held in memory.
- Every tag operation must be covered by automated tests, and the API documentation must stay in sync with the implementation.
- Management endpoints for tags must require the same API key authentication as the rest of the management API.
"""

CONSTRAINTS = [
    "no third-party runtime dependencies",
    "no changes to the existing storage schema",
    "tag data is carried in the existing link metadata field",
]


# --- the code this run produces ----------------------------------------------

_TAGS_MODULE = '''"""Tag normalisation, validation, and tag-based link search.

Tags live in the existing ``Link.metadata`` field, so this capability adds no
storage migration. The index is built on demand from a link iterable; at the
stated scale (10k links) a linear build is well inside the latency budget and
avoids a second source of truth that could drift from the store.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

from .errors import ValidationError

# 2 to 24 characters. The first and last must be alphanumeric, so a tag can
# never lead or trail with a hyphen; the middle allows hyphens.
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,22}[a-z0-9]$")
MAX_TAGS_PER_LINK = 8
METADATA_KEY = "tags"


def normalise_tag(raw: str) -> str:
    """Normalise a single tag, or raise :class:`ValidationError`."""
    if not isinstance(raw, str):
        raise ValidationError("tag must be a string", {"tag": repr(raw)})
    tag = raw.strip().lower()
    if not TAG_RE.match(tag):
        raise ValidationError(
            "tag must be 2-24 characters of lowercase letters, digits, or hyphens, "
            "and may not start or end with a hyphen",
            {"tag": raw},
        )
    return tag


def normalise_tags(raw: Optional[Sequence[str]]) -> List[str]:
    """Normalise a tag list, rejecting oversized sets and preserving order."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("tags must be a list of strings", {"tags": repr(raw)})
    if len(raw) > MAX_TAGS_PER_LINK:
        raise ValidationError(
            "a link may carry at most %d tags" % MAX_TAGS_PER_LINK,
            {"count": len(raw)},
        )
    seen: Dict[str, None] = {}
    for item in raw:
        seen.setdefault(normalise_tag(item), None)
    return list(seen)


def tags_of(link) -> List[str]:
    """Read the tags carried by a link, tolerating links that predate tagging."""
    value = (link.metadata or {}).get(METADATA_KEY)
    if not value:
        return []
    if isinstance(value, str):
        return [t for t in (part.strip() for part in value.split(",")) if t]
    return list(value)


class TagIndex:
    """Inverted index from tag to link codes, built from a link iterable."""

    def __init__(self, links: Iterable = ()) -> None:
        self._by_tag: Dict[str, List[str]] = {}
        self._order: Dict[str, float] = {}
        for link in links:
            self.add(link)

    def add(self, link) -> None:
        self._order[link.code] = link.created_at
        for tag in tags_of(link):
            self._by_tag.setdefault(tag, [])
            if link.code not in self._by_tag[tag]:
                self._by_tag[tag].append(link.code)

    def search(self, tag: str, limit: int = 50) -> List[str]:
        """Exact tag match, newest first."""
        return self._ordered(self._by_tag.get(normalise_tag(tag), []), limit)

    def search_prefix(self, prefix: str, limit: int = 50) -> List[str]:
        """Prefix match across tags, newest first, de-duplicated."""
        prefix = prefix.strip().lower()
        if not prefix:
            raise ValidationError("prefix must not be empty")
        codes: Dict[str, None] = {}
        for tag, entries in self._by_tag.items():
            if tag.startswith(prefix):
                for code in entries:
                    codes.setdefault(code, None)
        return self._ordered(list(codes), limit)

    def tags(self) -> List[str]:
        return sorted(self._by_tag)

    def _ordered(self, codes: List[str], limit: int) -> List[str]:
        if limit < 1:
            raise ValidationError("limit must be positive", {"limit": limit})
        ranked = sorted(codes, key=lambda c: (self._order.get(c, 0.0), c), reverse=True)
        return ranked[:limit]

    def __len__(self) -> int:
        return len(self._by_tag)
'''

_TAGS_TESTS = '''"""Tests for the tagging capability (generated by the greenfield run)."""

import time
import unittest

from service.errors import ValidationError
from service.models import Link
from service.tags import (
    MAX_TAGS_PER_LINK,
    TagIndex,
    normalise_tag,
    normalise_tags,
    tags_of,
)


def make_link(code, tags=None, created_at=None):
    return Link(
        code=code,
        target_url="https://example.com/%s" % code,
        created_at=created_at if created_at is not None else time.time(),
        metadata={"tags": tags} if tags is not None else {},
    )


class NormaliseTagTests(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(normalise_tag("  Marketing  "), "marketing")

    def test_accepts_digits_and_inner_hyphens(self):
        self.assertEqual(normalise_tag("q4-2026"), "q4-2026")

    def test_rejects_too_short(self):
        with self.assertRaises(ValidationError):
            normalise_tag("a")

    def test_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            normalise_tag("x" * 25)

    def test_rejects_leading_or_trailing_hyphen(self):
        for bad in ("-lead", "trail-"):
            with self.assertRaises(ValidationError):
                normalise_tag(bad)

    def test_rejects_non_string(self):
        with self.assertRaises(ValidationError):
            normalise_tag(42)


class NormaliseTagsTests(unittest.TestCase):
    def test_none_yields_empty(self):
        self.assertEqual(normalise_tags(None), [])

    def test_deduplicates_preserving_order(self):
        self.assertEqual(normalise_tags(["b1", "A2", "b1"]), ["b1", "a2"])

    def test_accepts_comma_separated_string(self):
        self.assertEqual(normalise_tags("aa, bb"), ["aa", "bb"])

    def test_rejects_more_than_max(self):
        with self.assertRaises(ValidationError):
            normalise_tags(["t%d" % i for i in range(MAX_TAGS_PER_LINK + 1)])

    def test_names_the_offending_tag(self):
        with self.assertRaises(ValidationError) as caught:
            normalise_tags(["good", "-bad"])
        self.assertEqual(caught.exception.details.get("tag"), "-bad")

    def test_rejects_wrong_container(self):
        with self.assertRaises(ValidationError):
            normalise_tags({"tags": ["a"]})


class TagsOfTests(unittest.TestCase):
    def test_missing_metadata_is_empty(self):
        self.assertEqual(tags_of(make_link("a")), [])

    def test_reads_list(self):
        self.assertEqual(tags_of(make_link("a", ["x1", "y2"])), ["x1", "y2"])

    def test_reads_comma_string(self):
        self.assertEqual(tags_of(make_link("a", "x1,y2")), ["x1", "y2"])


class TagIndexTests(unittest.TestCase):
    def setUp(self):
        self.links = [
            make_link("aaa", ["marketing", "q4-2026"], created_at=100.0),
            make_link("bbb", ["marketing"], created_at=200.0),
            make_link("ccc", ["engineering"], created_at=300.0),
            make_link("ddd", None, created_at=400.0),
        ]
        self.index = TagIndex(self.links)

    def test_exact_search_is_newest_first(self):
        self.assertEqual(self.index.search("marketing"), ["bbb", "aaa"])

    def test_search_normalises_the_query(self):
        self.assertEqual(self.index.search("  MARKETING "), ["bbb", "aaa"])

    def test_unknown_tag_is_empty(self):
        self.assertEqual(self.index.search("nope"), [])

    def test_untagged_links_are_absent(self):
        self.assertNotIn("ddd", self.index.search_prefix("m"))

    def test_prefix_search_spans_tags_without_duplicates(self):
        index = TagIndex([make_link("zzz", ["market", "marketing"], created_at=500.0)])
        self.assertEqual(index.search_prefix("mark"), ["zzz"])

    def test_prefix_search_orders_newest_first(self):
        self.assertEqual(self.index.search_prefix("e"), ["ccc"])

    def test_empty_prefix_rejected(self):
        with self.assertRaises(ValidationError):
            self.index.search_prefix("  ")

    def test_limit_is_applied(self):
        self.assertEqual(self.index.search("marketing", limit=1), ["bbb"])

    def test_non_positive_limit_rejected(self):
        with self.assertRaises(ValidationError):
            self.index.search("marketing", limit=0)

    def test_tags_listing_is_sorted(self):
        self.assertEqual(self.index.tags(), ["engineering", "marketing", "q4-2026"])

    def test_len_counts_distinct_tags(self):
        self.assertEqual(len(self.index), 3)

    def test_add_is_idempotent(self):
        self.index.add(self.links[0])
        self.assertEqual(self.index.search("marketing"), ["bbb", "aaa"])

    def test_search_at_scale_is_within_budget(self):
        many = [make_link("c%05d" % i, ["bulk"], created_at=float(i)) for i in range(10000)]
        index = TagIndex(many)
        started = time.perf_counter()
        found = index.search("bulk", limit=50)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(found), 50)
        self.assertLess(elapsed, 0.05, "tag search must stay under the 50ms budget")


if __name__ == "__main__":
    unittest.main()
'''


def change_plan() -> ChangePlan:
    return ChangePlan(
        summary="Add link tagging: normalisation, validation, and tag/prefix search.",
        requirement_ids=["FR-1", "FR-2", "FR-3", "FR-4"],
        rollback_note=(
            "Both files are new and nothing existing imports them, so deleting "
            "service/tags.py and tests/test_tags.py restores the prior state exactly."
        ),
        changes=[
            FileChange(path="service/tags.py", action="create", content=_TAGS_MODULE,
                       description="tag normalisation, validation, and inverted index"),
            FileChange(path="tests/test_tags.py", action="create", content=_TAGS_TESTS,
                       description="unit tests covering validation, search, ordering, and the latency budget"),
        ],
    )


def _task_node_factory(task) -> Optional[Node]:
    """Turn a planned task into an executable node injected at runtime."""
    return Node(
        id="spec-%s" % task.id,
        stage=Stage.PLANNING,
        agent=TaskSpecAgent(task),
        description="Specify %s" % task.title,
        depends_on=["decompose"],
        exit_gates=[AgentSucceeded()],
        # Specification is analysis; it must not mutate the workspace.
        autonomy=AutonomyLevel.PROPOSE_ONLY,
        on_failure=FailureAction.CONTINUE,
    )


def build(inputs: Optional[Dict[str, Any]] = None) -> WorkflowGraph:
    nodes: List[Node] = [
        Node(
            id="requirements", stage=Stage.REQUIREMENTS,
            agent=RequirementsAgent(ambiguity_threshold=0.30),
            description="Normalise the requirement and surface ambiguity.",
            entry_gates=[ContextKeysPresent("requirement")],
            exit_gates=[
                AgentSucceeded(),
                ProducedOutputs("requirements", "acceptance_criteria"),
                MetricThreshold("ambiguity_score", maximum=0.30),
            ],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="architecture", stage=Stage.ARCHITECTURE, agent=ArchitectAgent(),
            description="Derive components, API contract, and decision records.",
            depends_on=["requirements"],
            # Designing against an ambiguous requirement produces a confident
            # design for the wrong system, so the gate blocks rather than warns.
            entry_gates=[NoOpenQuestions()],
            exit_gates=[AgentSucceeded(), ProducedOutputs("design", "adrs")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="decompose", stage=Stage.PLANNING,
            agent=PlannerAgent(node_factory=_task_node_factory, barrier="implement"),
            description="Decompose into tasks and inject them into the running graph.",
            depends_on=["requirements", "architecture"],
            exit_gates=[AgentSucceeded(), ProducedOutputs("task_plan")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="implement", stage=Stage.IMPLEMENTATION,
            agent=ImplementationAgent(change_plan()),
            description="Apply the reviewed change plan to the workspace.",
            depends_on=["decompose"],
            entry_gates=[ContextKeysPresent("task_plan", "design")],
            exit_gates=[AgentSucceeded(), ProducedOutputs("changed_files")],
            retry=RetryPolicy(max_attempts=2, backoff_seconds=0.2),
            on_failure=FailureAction.ROLLBACK,
            autonomy=AutonomyLevel.ACT_AND_REPORT,
            policy_tags=["change_control"],
        ),
    ]
    nodes.extend(verification_tail(depends_on=["implement"], min_coverage=55.0, version="1.2.0"))

    return WorkflowGraph(
        name="greenfield",
        description="Build link tagging and tag search from nothing, end to end.",
        nodes=nodes,
    )


def default_inputs() -> Dict[str, Any]:
    return {
        "requirement": REQUIREMENT.strip(),
        "constraints": list(CONSTRAINTS),
        # The requirement is well specified, but the detector still asks about
        # unstated non-functional dimensions. These are the answers a product
        # owner would give, recorded so they are auditable rather than assumed.
        "clarifications": {
            "Q-NFR-PERFORMANCE": "tag search p99 under 50ms for 10k in-memory links",
            "Q-NFR-AVAILABILITY": "inherits the service SLO; tagging adds no new failure domain",
            "Q-NFR-SECURITY": "management endpoints keep the existing API-key requirement",
            "Q-NFR-SCALE": "10k links per account, 8 tags per link",
            "Q-NFR-DATA_RETENTION": "tags are deleted with their link; no separate retention",
            "Q-NFR-OBSERVABILITY": "tag search latency is reported through the existing metrics registry",
        },
    }
