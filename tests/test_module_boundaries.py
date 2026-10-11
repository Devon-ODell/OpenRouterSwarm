"""P13 — module boundary enforcement.

Each test here parses module sources with `ast` (never imports them) so that
aliased, relative and try/except-guarded imports are all caught.
"""
import ast
import unittest
from pathlib import Path

SWARM = Path(__file__).resolve().parents[1] / "swarm"


def imported_modules(name):
    """Every swarm-local module `swarm/<name>.py` imports, by bare module name, wherever the
    import statement sits (top level, try/except, inside a function)."""
    tree = ast.parse((SWARM / f"{name}.py").read_text())
    found = set()

    def bare(alias_name):
        return alias_name[6:] if alias_name.startswith("swarm.") else alias_name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(bare(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:  # relative
                if node.module is None:
                    for alias in node.names:
                        found.add(alias.name)
                else:
                    found.add(node.module)
            else:  # absolute
                if node.module == "swarm":
                    for alias in node.names:
                        found.add(alias.name)
                elif node.module and node.module.startswith("swarm."):
                    found.add(node.module.split(".")[1])
                elif node.module:
                    found.add(node.module.split(".")[0])
    return {n for n in found if (SWARM / f"{n}.py").exists()}


def stdlib_imports(name):
    """Bare names of non-swarm top-level modules imported (level-0 only)."""
    tree = ast.parse((SWARM / f"{name}.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def call_attrs(name):
    """All attribute names used as the callee of a Call, e.g. `x.release()` -> 'release'."""
    tree = ast.parse((SWARM / f"{name}.py").read_text())
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def names_named(name, targets):
    """ast.Name / ast.Attribute identifiers that appear in targets."""
    tree = ast.parse((SWARM / f"{name}.py").read_text())
    hit = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in targets:
            hit.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in targets:
            hit.add(node.attr)
    return hit


LAYERS = [
    # 0: leaves — pure helpers, records, prompts, policy tables
    {"runtime_context", "prompts", "task_rules", "context_builder", "workflow", "learn",
     "metrics", "pacing", "budget", "budget_requests", "sandbox", "swallow", "classify_no_change",
     "corpus_index", "mit_corpus", "experiment", "hardware", "hooks", "extensions", "routing",
     "receipts", "undo", "wallet", "providers"},
    # 1: services
    {"task_queue", "workspace", "verification", "provisioning", "agent_turn"},
    # 2: one attempt
    {"attempt_runner"},
    # 3: the worker loop
    {"worker"},
    # 4: planning, roadmap, reporting
    {"planning", "roadmap", "reporting"},
    # 5: lifecycle
    {"supervisor"},
    # 6: application facade and CLI parsing
    {"app", "cli"},
    # 7: entry points
    {"swarmd", "bridge"},
]
LAYER_OF = {mod: i for i, layer in enumerate(LAYERS) for mod in layer}
ALL_SWARM_MODULES = sorted(p.stem for p in SWARM.glob("*.py"))


class ModuleBoundaryTests(unittest.TestCase):

    def test_no_production_module_imports_swarmd(self):
        # bridge is the panel's command surface over the daemon; it reaches the daemon only
        # through swarm.swarmd's public names and SwarmApplication (P12). It leaves this list
        # once the cmd_* surface moves out of swarmd.
        allowlist = {"bridge"}
        for name in ALL_SWARM_MODULES:
            if name == "swarmd":
                continue
            if name in allowlist:
                continue
            if "swarmd" in imported_modules(name):
                self.fail(f"{name}.py imports swarmd")

    def test_prompts_imports_no_runtime_state(self):
        banned = {"swarmd", "runtime_context", "task_queue", "workspace",
                  "supervisor", "worker", "attempt_runner"}
        hit = imported_modules("prompts") & banned
        self.assertEqual(
            hit, set(),
            f"prompts.py imports runtime state: {sorted(hit)}")

    def test_task_queue_imports_no_git_or_provider_module(self):
        banned = {"workspace", "providers", "agent_turn", "swarmd"}
        hit = imported_modules("task_queue") & banned
        self.assertEqual(
            hit, set(),
            f"task_queue.py imports forbidden module: {sorted(hit)}")
        std = stdlib_imports("task_queue")
        self.assertNotIn(
            "subprocess", std,
            "task_queue.py imports subprocess")

    def test_verification_performs_no_queue_writes(self):
        hit = imported_modules("verification") & {"task_queue", "swarmd"}
        self.assertEqual(
            hit, set(),
            f"verification.py imports queue-writer module: {sorted(hit)}")
        bad = call_attrs("verification") & {"release", "claim", "strand", "repoint", "requeue"}
        self.assertEqual(
            bad, set(),
            f"verification.py performs queue write: {sorted(bad)}")

    def test_workspace_contains_no_retry_policy_decisions(self):
        hit = imported_modules("workspace") & {"task_queue", "task_rules", "learn"}
        self.assertEqual(
            hit, set(),
            f"workspace.py imports retry-policy module: {sorted(hit)}")
        bad = names_named("workspace", {"MAX_ATTEMPTS", "failure_class", "TaskPolicy"})
        self.assertEqual(
            bad, set(),
            f"workspace.py references retry-policy name: {sorted(bad)}")

    def test_reporting_performs_no_queue_mutations(self):
        hit = imported_modules("reporting") & {"task_queue", "swarmd"}
        self.assertEqual(
            hit, set(),
            f"reporting.py imports queue-mutating module: {sorted(hit)}")
        # grep -n '\.add(\|\.release(\|\.claim(' swarm/reporting.py  -> no legitimate
        # local set/dict `.add(` exists, so no line-range allow-list is needed.
        bad = call_attrs("reporting") & {"add", "release", "claim", "strand",
                                         "repoint", "requeue", "dismiss"}
        self.assertEqual(
            bad, set(),
            f"reporting.py mutates a queue: {sorted(bad)}")

    def test_lower_layers_never_import_higher_layers(self):
        for name in ALL_SWARM_MODULES:
            my_layer = LAYER_OF[name]
            for dep in imported_modules(name):
                if dep not in LAYER_OF:
                    continue
                if LAYER_OF[dep] > my_layer:
                    self.fail(
                        f"{name}.py (layer {my_layer}) imports higher-layer "
                        f"{dep} (layer {LAYER_OF[dep]})")

    def test_every_module_assigned_to_exactly_one_layer(self):
        seen = {}
        for mod in ALL_SWARM_MODULES:
            layers = [i for i, layer in enumerate(LAYERS) if mod in layer]
            self.assertEqual(
                len(layers), 1,
                f"{mod}.py is in {len(layers)} layers: {layers}")
            seen[mod] = layers[0]
        self.assertEqual(set(seen), set(ALL_SWARM_MODULES),
                         "some swarm module has no layer assignment")


class BridgeFacadeTests(unittest.TestCase):
    """The bridge selects repositories and reads their records through SwarmApplication. Pure
    helpers (trunk_name, scope_gap, KINDS, ...) may still be called on the daemon module."""

    STATEFUL = {"use_repo", "Queue", "queue_for", "attempts_for", "landed_commits", "health",
                "read_now", "idle_state", "read_roadmap", "daemon_note", "import_bugs",
                "build_report", "_read", "STATE", "LOGS", "CONFIG"}

    def test_bridge_reaches_daemon_state_only_through_the_facade(self):
        tree = ast.parse((SWARM / "bridge.py").read_text())
        # Queue.blocked_by is a pure static helper over rows the caller already holds.
        pure = {id(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "blocked_by"}
        hits = sorted({f"swarmd.{n.attr} (line {n.lineno})" for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id == "swarmd" and n.attr in self.STATEFUL
                       and id(n) not in pure})
        self.assertEqual(hits, [])
