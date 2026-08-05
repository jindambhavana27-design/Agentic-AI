"""Brownfield codebase reasoning.

Static analysis over the real source tree, not a description of it. The agent
parses every module with :mod:`ast` and derives:

* the **module import graph**, and from it the reverse-dependency closure of a
  change target -- the actual blast radius rather than a guess;
* the **HTTP route table**, extracted from the router registrations, which is
  what makes API-contract drift detectable;
* **layering violations**, by checking imports against the declared layer order;
* the **tests that cover** each impacted module, so a change can be told whether
  it is walking into untested territory.

Everything reported here is derived from parsed code. Where analysis is
structurally incapable of seeing something -- dynamic imports, reflection -- that
is stated rather than papered over.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..types import AgentResult, Severity
from .base import Agent, AgentContext

# Lower index == lower layer. An import from a lower layer to a higher one
# inverts the dependency direction and is reported.
DEFAULT_LAYERS = ["models", "errors", "config", "validation", "observability",
                  "ratelimit", "storage", "analytics", "shortener", "app", "server"]


@dataclass
class ModuleInfo:
    module: str
    path: str
    imports: Set[str] = field(default_factory=set)
    functions: List[str] = field(default_factory=list)
    classes: List[str] = field(default_factory=list)
    loc: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module, "path": self.path, "loc": self.loc,
            "imports": sorted(self.imports), "functions": self.functions,
            "classes": self.classes,
        }


@dataclass
class RouteInfo:
    method: str
    pattern: str
    name: str
    auth_required: bool = False

    @property
    def path(self) -> str:
        return _pattern_to_path(self.pattern)

    def to_dict(self) -> Dict[str, Any]:
        return {"method": self.method, "pattern": self.pattern, "path": self.path,
                "name": self.name, "auth_required": self.auth_required}


class ImpactAnalysisAgent(Agent):
    name = "impact-analyst"
    description = "Maps the blast radius of a change across modules, APIs, and tests."
    reads = ("change_target", "requirements", "package")
    writes = ("impact_analysis", "impacted_modules", "route_table", "existing_components",
              "test_coverage_map")

    def __init__(self, package: str = "service", test_dir: str = "tests",
                 layers: Optional[Sequence[str]] = None) -> None:
        self.package = package
        self.test_dir = test_dir
        self.layers = list(layers or DEFAULT_LAYERS)

    def execute(self, ctx: AgentContext) -> AgentResult:
        root = ctx.workspace
        package_dir = os.path.join(root, self.package)
        if not os.path.isdir(package_dir):
            return self.failed("package %r not found under %s" % (self.package, root))

        modules = _scan_package(package_dir, self.package)
        if not modules:
            return self.failed("no Python modules found in %s" % package_dir)

        targets = _normalise_targets(ctx.read("change_target", []) or [], modules)
        reverse = _reverse_dependencies(modules)
        impacted = _closure(targets, reverse)

        routes = _extract_routes(os.path.join(package_dir, "app.py"))
        tests = _scan_tests(os.path.join(root, self.test_dir), self.package)
        coverage_map = {
            module: sorted(t for t, imported in tests.items() if _reaches(module, imported))
            for module in sorted(impacted or modules)
        }

        violations = self._layering_violations(modules)
        findings: List[Any] = []
        for violation in violations:
            findings.append(self.finding(
                Severity.MEDIUM,
                "layering violation: %s imports %s (higher layer)" % violation,
                category="architecture", location=violation[0],
                remediation="invert the dependency or move the shared type to a lower layer"))

        untested = [m for m in impacted if not coverage_map.get(m)]
        for module in untested:
            findings.append(self.finding(
                Severity.HIGH,
                "impacted module %s has no direct test coverage" % module,
                category="test-coverage", location=module,
                remediation="add a test that imports and exercises this module before changing it"))

        risk = _risk_score(impacted, modules, untested, routes, targets)
        analysis = {
            "package": self.package,
            "modules_total": len(modules),
            "targets": sorted(targets),
            "impacted_modules": sorted(impacted),
            "blast_radius_ratio": round(len(impacted) / len(modules), 3) if modules else 0.0,
            "routes": [r.to_dict() for r in routes],
            "untested_impacted": untested,
            "layering_violations": [{"from": a, "to": b} for a, b in violations],
            "risk_score": risk,
            "analysis_limits": [
                "static import analysis only; dynamic imports and reflection are invisible",
                "test mapping is by import, not by executed lines -- it proves reachability, not coverage",
            ],
        }

        document = _render_report(analysis, modules, coverage_map)

        return self.ok(
            outputs={
                "impact_analysis": analysis,
                "impacted_modules": sorted(impacted),
                "route_table": [r.to_dict() for r in routes],
                "existing_components": sorted(modules),
                "test_coverage_map": coverage_map,
            },
            artifacts=[self.document("impact-analysis", "docs/impact-analysis.md", document)],
            findings=findings,
            metrics={
                "modules_scanned": len(modules),
                "modules_impacted": len(impacted),
                "blast_radius_ratio": analysis["blast_radius_ratio"],
                "routes_found": len(routes),
                "untested_impacted": len(untested),
                "layering_violations": len(violations),
                "risk_score": risk,
            },
            rationale="%d of %d module(s) impacted by %s (risk %.2f)"
                      % (len(impacted), len(modules), ", ".join(sorted(targets)) or "no target", risk),
        )

    def _layering_violations(self, modules: Dict[str, ModuleInfo]) -> List[Tuple[str, str]]:
        def rank(module: str) -> Optional[int]:
            leaf = module.split(".")[-1]
            for index, layer in enumerate(self.layers):
                if leaf == layer or module.endswith("." + layer):
                    return index
            # Sub-packages inherit their package's rank (storage.sqlite_store).
            for index, layer in enumerate(self.layers):
                if ("." + layer + ".") in module + ".":
                    return index
            return None

        violations: List[Tuple[str, str]] = []
        for module, info in modules.items():
            source_rank = rank(module)
            if source_rank is None:
                continue
            for imported in info.imports:
                if not imported.startswith(self.package):
                    continue
                target_rank = rank(imported)
                if target_rank is not None and target_rank > source_rank:
                    violations.append((module, imported))
        return sorted(set(violations))


# -- static analysis ----------------------------------------------------------


def _scan_package(package_dir: str, package: str) -> Dict[str, ModuleInfo]:
    modules: Dict[str, ModuleInfo] = {}
    for dirpath, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, os.path.dirname(package_dir))
            module = rel[:-3].replace(os.sep, ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            info = _parse_module(path, module, package)
            if info:
                modules[module] = info
    return modules


def _parse_module(path: str, module: str, package: str) -> Optional[ModuleInfo]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
    except (OSError, SyntaxError):
        return None

    info = ModuleInfo(module=module, path=path, loc=len(source.splitlines()))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                info.imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            info.imports.add(_resolve_import_from(node, module, package))
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            info.functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            info.classes.append(node.name)
    info.imports.discard("")
    return info


def _resolve_import_from(node: ast.ImportFrom, module: str, package: str) -> str:
    """Resolve a relative import to an absolute module path."""
    if node.level == 0:
        return node.module or ""
    parts = module.split(".")
    # level 1 == current package, level 2 == parent, and so on.
    base = parts[: max(0, len(parts) - node.level)]
    if node.module:
        base = base + node.module.split(".")
    resolved = ".".join(base)
    return resolved or package


def _reverse_dependencies(modules: Dict[str, ModuleInfo]) -> Dict[str, Set[str]]:
    """module -> set of modules that import it (directly)."""
    reverse: Dict[str, Set[str]] = {m: set() for m in modules}
    for module, info in modules.items():
        for imported in info.imports:
            # An import of `service.storage.sqlite_store` also depends on
            # `service.storage`, so credit every matching prefix.
            for candidate in modules:
                if imported == candidate or imported.startswith(candidate + "."):
                    reverse[candidate].add(module)
    return reverse


def _closure(targets: Set[str], reverse: Dict[str, Set[str]]) -> Set[str]:
    """Transitive set of modules affected by changing ``targets``."""
    impacted: Set[str] = set(targets)
    frontier = list(targets)
    while frontier:
        current = frontier.pop()
        for dependent in reverse.get(current, ()):  # noqa: B007
            if dependent not in impacted:
                impacted.add(dependent)
                frontier.append(dependent)
    return impacted


def _normalise_targets(raw: Any, modules: Dict[str, ModuleInfo]) -> Set[str]:
    if isinstance(raw, str):
        raw = [raw]
    targets: Set[str] = set()
    for item in raw or []:
        text = str(item).replace("/", ".").replace(".py", "")
        for module in modules:
            if module == text or module.endswith("." + text) or text.endswith(module):
                targets.add(module)
    return targets


def _extract_routes(app_path: str) -> List[RouteInfo]:
    """Pull the route table out of the router registrations.

    Reads the code rather than importing it: importing an application module to
    inspect it runs arbitrary top-level code, which an analysis pass must not do.
    """
    if not os.path.exists(app_path):
        return []
    try:
        with open(app_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=app_path)
    except (OSError, SyntaxError):
        return []

    routes: List[RouteInfo] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Matches both `r(...)` and `self.route(...)`.
        is_route_call = (
            (isinstance(func, ast.Name) and func.id == "r")
            or (isinstance(func, ast.Attribute) and func.attr == "route")
        )
        if not is_route_call or len(node.args) < 2:
            continue
        method = _literal(node.args[0])
        pattern = _literal(node.args[1])
        name = _literal(node.args[2]) if len(node.args) > 2 else ""
        if not method or not pattern:
            continue
        auth = any(
            kw.arg == "auth_required" and isinstance(kw.value, ast.Constant) and kw.value.value
            for kw in node.keywords
        )
        routes.append(RouteInfo(method=method, pattern=pattern, name=name or "", auth_required=auth))
    return routes


def _literal(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _pattern_to_path(pattern: str) -> str:
    """Turn a route regex into an OpenAPI-style path."""
    import re as _re

    path = pattern.strip("^$")
    path = _re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", path)
    path = _re.sub(r"\([^)]*\)", "{param}", path)
    return path.replace("\\", "")


def _reaches(module: str, imported: Set[str]) -> bool:
    """Whether a test importing ``imported`` reaches ``module``.

    Importing a package runs its ``__init__``, so a test that imports
    ``service.storage`` does execute ``service.storage.memory_store`` when the
    package re-exports it. Requiring an exact import would report false coverage
    gaps for every module reached through its package.
    """
    if module in imported:
        return True
    return any(name.startswith(module + ".") or module.startswith(name + ".")
               for name in imported)


def _scan_tests(test_dir: str, package: str) -> Dict[str, Set[str]]:
    """test file -> set of package modules it imports."""
    mapping: Dict[str, Set[str]] = {}
    if not os.path.isdir(test_dir):
        return mapping
    for filename in sorted(os.listdir(test_dir)):
        if not (filename.startswith("test_") and filename.endswith(".py")):
            continue
        path = os.path.join(test_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        imported: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(package):
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(package):
                        imported.add(alias.name)
        mapping[filename] = imported
    return mapping


def _risk_score(impacted: Set[str], modules: Dict[str, ModuleInfo],
                untested: List[str], routes: List[RouteInfo], targets: Set[str]) -> float:
    """Blend blast radius, coverage gaps, and public-surface exposure into 0..1."""
    if not modules:
        return 0.0
    radius = len(impacted) / len(modules)
    coverage_gap = len(untested) / max(1, len(impacted))
    surface = 1.0 if any("app" in t or "server" in t for t in targets) else 0.0
    score = 0.45 * radius + 0.35 * coverage_gap + 0.20 * surface
    return round(min(1.0, score), 3)


def _render_report(analysis: Dict[str, Any], modules: Dict[str, ModuleInfo],
                   coverage_map: Dict[str, List[str]]) -> str:
    lines = ["# Impact Analysis", "",
             "Change target(s): %s" % (", ".join("`%s`" % t for t in analysis["targets"]) or "_none_"),
             "",
             "- modules scanned: **%d**" % analysis["modules_total"],
             "- modules impacted: **%d** (blast radius %.0f%%)"
             % (len(analysis["impacted_modules"]), analysis["blast_radius_ratio"] * 100),
             "- composite risk score: **%.2f**" % analysis["risk_score"],
             "", "## Impacted modules and their test reachability", "",
             "| Module | LOC | Tests importing it |", "| --- | --- | --- |"]
    for module in analysis["impacted_modules"]:
        info = modules.get(module)
        tests = coverage_map.get(module) or []
        lines.append("| `%s` | %d | %s |" % (
            module, info.loc if info else 0,
            ", ".join("`%s`" % t for t in tests) or "**none**",
        ))
    if analysis["routes"]:
        lines += ["", "## API surface", "", "| Method | Path | Auth | Handler |",
                  "| --- | --- | --- | --- |"]
        for route in analysis["routes"]:
            lines.append("| %s | `%s` | %s | `%s` |" % (
                route["method"], route["path"], "yes" if route["auth_required"] else "no",
                route["name"]))
    if analysis["layering_violations"]:
        lines += ["", "## Layering violations", ""]
        for violation in analysis["layering_violations"]:
            lines.append("- `%s` imports `%s`" % (violation["from"], violation["to"]))
    lines += ["", "## Analysis limits", ""]
    lines += ["- %s" % limit for limit in analysis["analysis_limits"]]
    return "\n".join(lines) + "\n"
