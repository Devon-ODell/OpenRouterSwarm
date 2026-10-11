"""Provisioning: repository detection, `swarm init` surroundings, prov recipes.

Extracted from swarmd (P2). No module reads swarmd globals: config I/O (load_cfg,
save_cfg, python_for, log) and git identity arrive as explicit parameters.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path



def _default_git_branch(repo):
    """The current branch of a repo, or "main" (same rule as the daemon git_branch)."""
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out and out != "HEAD" else "main"



def detect_test_cmd(repo, log=None):
    """How to test this repository, or None. A folder that only holds projects (no manifest of
    its own) is searched one level down: a single project there is used, several are ambiguous
    and left to the owner, since testing the wrong one would reject every task."""
    repo = Path(repo)
    cmd = project_test_cmd(repo)
    if cmd:
        return cmd
    found = sub_projects(repo)
    if len(found) == 1:
        name, cmd = found[0]
        if log is not None:
            log(f"no project at the top level; testing the only one inside it: {name}")
        return f"cd {shlex.quote(name)} && {cmd}"
    return None


def embedded_repos(repo):
    """Directories one level inside repo that are Git repositories of their own.

    Git never stores an embedded repository's files in its parent, so those directories come
    out EMPTY in every worktree the swarm builds. The swarm cannot read or change that code
    from here: it has to be pointed at the inner repository instead."""
    return [d.name for d in _subdirs(repo) if (d / ".git").exists()]


def _subdirs(repo):
    try:
        return sorted(d for d in Path(repo).iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return []


def sub_projects(repo):
    """[(directory name, its test command)] for projects one level inside repo, skipping
    embedded repositories, whose code a worktree of this repository would not contain."""
    gone = set(embedded_repos(repo))
    return [(d.name, cmd) for d in _subdirs(repo) if d.name not in gone
            for cmd in [project_test_cmd(d)] if cmd]


def test_cmd_hint(repo):
    """What to tell someone whose repository the swarm cannot test by itself."""
    repo, lines = Path(repo), []
    for name in embedded_repos(repo):
        lines += [f"  {name}/ is its own Git repository, so its code is not part of {repo.name} "
                  f"and never appears in the swarm's worktrees.",
                  f"  Point the swarm at it directly:",
                  f"    swarm grind {shlex.quote(str(repo / name))} --goal '...'"]
    found = sub_projects(repo)
    if found:
        lines.append("  Projects inside it, and how each would be tested:")
        lines += [f"    --test-cmd 'cd {shlex.quote(n)} && {c}'" for n, c in found]
        lines.append("  Point the swarm at one of those directories instead, or pass one of the above.")
    if not lines:
        lines = ["  Nothing recognisable to test (no go.mod, package.json, pyproject.toml, "
                 "Cargo.toml or Makefile test target).",
                 "  Pass the command you run yourself, e.g. --test-cmd 'go test ./...'"]
    return "\n".join(lines)


def test_cmds(repo):
    """Every way this directory could be tested, best guess first: [(what found it, command)].

    A repository with code in two languages matches more than once — a Go service with a
    Python notebook beside it — so the runner-up is worth showing when the first choice
    turns out to be the wrong one."""
    repo, out = Path(repo), []
    if (repo / "go.mod").exists():
        out.append(("go.mod", "go test ./..."))
    if (repo / "Cargo.toml").exists():
        out.append(("Cargo.toml", "cargo test -q"))
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            test = json.loads(pkg.read_text()).get("scripts", {}).get("test", "")
        except (OSError, json.JSONDecodeError):
            test = ""
        if test and "no test specified" not in test:
            if (repo / "pnpm-lock.yaml").exists():
                out.append(("package.json", "pnpm install --frozen-lockfile --silent && pnpm test"))
            elif (repo / "yarn.lock").exists():
                out.append(("package.json", "yarn install --frozen-lockfile --silent && yarn test"))
            elif (repo / "package-lock.json").exists():
                out.append(("package.json", "npm ci --silent --no-audit --no-fund && npm test --silent"))
            else:
                out.append(("package.json", "npm install --silent --no-audit --no-fund && npm test --silent"))
    found = next((f for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tests")
                  if (repo / f).exists()), None)
    if found:
        r = subprocess.run(["python3", "-c", "import pytest"], capture_output=True)
        out.append((found, "python3 -m pytest -q" if r.returncode == 0
                    else "python3 -m unittest discover -q"))
    mk = repo / "Makefile"
    if mk.exists() and re.search(r"^test:", mk.read_text(errors="replace"), re.M):
        out.append(("Makefile", "make test"))
    return out


def project_test_cmd(repo):
    cmds = test_cmds(repo)
    return cmds[0][1] if cmds else None


def other_test_cmds(repo, current):
    """Ways to test this repository other than the one that just failed, as printable lines."""
    repo = Path(repo)
    rows = [(what, cmd) for what, cmd in test_cmds(repo) if cmd != current]
    rows += [(f"{name}/", f"cd {shlex.quote(name)} && {cmd}") for name, cmd in sub_projects(repo)
             if f"cd {shlex.quote(name)} && {cmd}" != current]
    if not rows:
        return ""
    lines = ["\n  Other ways this repository could be tested:"]
    lines += [f"    --test-cmd {shlex.quote(cmd)}   (found {what})" for what, cmd in rows]
    return "\n".join(lines)


def scaffold(path, goal, git_ident=None):
    """A new Git repository for a problem that has no codebase yet."""
    d = Path(path).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    if (d / ".git").exists():
        return d
    (d / "tests").mkdir(exist_ok=True)
    (d / "README.md").write_text(f"# {d.name}\n\n{goal}\n")
    (d / "GOAL.md").write_text(f"# Goal\n\n{goal}\n")
    (d / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\nnode_modules/\n.pytest_cache/\ndist/\nbuild/\n")
    (d / "tests" / "__init__.py").write_text("")
    (d / "tests" / "test_smoke.py").write_text(
        "import unittest\n\n\nclass Smoke(unittest.TestCase):\n"
        "    def test_suite_runs(self):\n        self.assertTrue(True)\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n")
    env = {**os.environ, **(git_ident or {})}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "swarm: scaffold"]):
        subprocess.run(["git", *args], cwd=d, check=True, capture_output=True, env=env)
    return d


def configure(repo, test_cmd=None, load_cfg=None, save_cfg=None, python_for=None, log=None):
    repo = Path(repo).expanduser().resolve()
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                      capture_output=True).returncode != 0:
        sys.exit(f"{repo} needs a Git repository with at least one commit.\n"
                 f"  git -C '{repo}' init && git -C '{repo}' add -A && git -C '{repo}' commit -m baseline")
    if load_cfg is None:
        load_cfg = lambda: {}
    c = load_cfg()
    if c.get("repo") != str(repo):
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        c.update(repo=str(repo), base_branch=branch if branch and branch != "HEAD" else "main")
        c["test_cmd"] = test_cmd or detect_test_cmd(repo) or "__TEST_CMD__"
    elif test_cmd:
        c["test_cmd"] = test_cmd
    if python_for is not None:
        c["python"] = python_for(c)
    if save_cfg is not None:
        save_cfg(c)
    if c["test_cmd"].startswith("__"):
        sys.exit(f"could not detect how to test {repo}.\n{test_cmd_hint(repo)}")
    if log is not None:
        log(f"target {repo} on {c.get('base_branch')}; tests: {c.get('test_cmd')}")


def why_unrunnable(cmd):
    """Why a test command could not run at all, as a line to append to an error, or "".

    Judged from the command itself: its output only says "not found", which is equally true of
    a missing interpreter and of prose passed by mistake. Only a single program can be checked,
    so a compound shell command is left alone."""
    if re.search(r"[|&;<>()`]|\$\(", cmd):
        return ""
    try:
        first = shlex.split(cmd)[0]
    except (ValueError, IndexError):
        return ""
    if shutil.which(first):
        return ""
    if not re.fullmatch(r"[\w.+-]+(/[\w.+-]+)*", first):
        return ("\n  --test-cmd takes a shell command that runs the tests, not a description of "
                "the work; the goal goes in --goal.")
    instead = {"python": "python3", "pip": "pip3"}.get(first)
    return (f"\n  `{first}` is not installed or not on PATH"
            + (f" — macOS ships `{instead}`, not `{first}`." if instead else "."))



# Repository shapes that tell `swarm init` what a bare worktree will be missing and therefore
# what provisioning hooks it needs. Keyed by marker file; `name` is shown to the human.
RUNTIME_MARKERS = [
    ("requirements.txt", "python deps"),
    ("pyproject.toml", "python deps"),
    ("Pipfile", "python deps"),
    ("package.json", "node deps"),
    ("Gemfile", "ruby deps"),
    ("go.mod", "go deps"),
    ("Cargo.toml", "rust deps"),
    ("build.gradle", "gradle deps"),
    ("pom.xml", "maven deps"),
    ("composer.json", "php deps"),
    ("mix.exs", "elixir deps"),
]

# Marker -> proven installer command for a bare checkout. Verified against the dj-bot repo,
# whose postinstall writes a fix-ups shim that a fresh npm ci needs in place before `npm run
# check` can resolve "@strudel/core".
PROV_RECIPES = [
    (("package.json",), "npm ci --silent --no-audit --no-fund"),
    (("requirements.txt", "pyproject.toml", "Pipfile"), {
        "requirements.txt": ("python3 -m venv .venv && "
                             ".venv/bin/python -m pip install -q -r requirements.txt"),
        "pyproject.toml": ("python3 -m venv .venv && "
                           ".venv/bin/python -m pip install -q -e ."),
        "Pipfile": ("python3 -m venv .venv && "
                    ".venv/bin/python -m pip install -q pipenv && "
                    ".venv/bin/pipenv install --dev"),
    }),
    (("Gemfile",), "bundle install"),
    (("go.mod",), "go mod download"),
    (("Cargo.toml",), "cargo build"),
    (("build.gradle", "pom.xml"), "mvn -q -DskipTests dependency:go-offline"),
    (("composer.json",), "composer install --quiet"),
    (("mix.exs",), "mix deps.get"),
]



def probe_surroundings(repo):
    """A machine-readable snapshot of what a bare worktree of `repo` will lack.

    Returns {"nested": [...], "runtime": {"python": bool, "node": bool, ...},
    "venv": bool, "node_modules": bool, "gitignore": [...]}. Language markers are
    searched one level deep as well as at the top: several swarm targets keep their whole
    project (package.json, requirements.txt) under a subdirectory like `spike/` or
    `server/`, and a bare worktree loses `node_modules/` wherever it lives. This is what
    `swarm init` drops next to its tuned config, and what the baseline gate reads to fail
    fast when the gate needs something no prov hook installs."""
    repo = Path(repo)
    one_deep = [p for p in repo.iterdir() if p.is_dir() and not p.name.startswith(".")
                and not (p / ".git").exists()]
    nested = [d.name for d in sorted(p for p in repo.iterdir() if p.is_dir() and (p / ".git").exists())]
    gitignore = []
    gi = repo / ".gitignore"
    if gi.is_file():
        for line in gi.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                gitignore.append(line)
    # The marker file exists if it is at the top level OR one level down in any subdir.
    deps = {}
    subdirs = {}
    for name, _ in RUNTIME_MARKERS:
        deps[name] = (repo / name).is_file() or any((d / name).is_file() for d in one_deep)
        if deps[name] and not (repo / name).is_file():
            subdirs[name] = next(d.name for d in one_deep if (d / name).is_file())
    node_modules = (repo / "node_modules").exists() or any(
        (d / "node_modules").exists() for d in one_deep)
    return {
        "nested": nested,
        "nested_sources": {d.name: str(d.resolve()) for d in sorted(
            p for p in repo.iterdir() if p.is_dir() and (p / ".git").exists())},
        "runtime": deps,
        "subdirs": subdirs,
        "venv": (repo / ".venv").exists(),
        "node_modules": node_modules,
        "gitignore": gitignore,
    }


def build_prov_block(s, test_cmd):
    """The prov hooks a fresh worktree needs, from the surroundings probe.

    Two sources: the language markers (requiring an install that the test command itself
    may not perform), and the nested repositories listed by the probe (each one is absent
    from a bare worktree and must be bootstrapped by a hook). If the test command already
    runs the same install (an npm ci before npm run check, say), that recipe is skipped —
    the hook list stays minimal and idempotent. Skip tokens are per-language so an "npm
    ci" gate does not suppress the python-requirements hook and vice versa."""
    # A recipe is skipped when the gate already performs that language's install.
    # Keyed by marker file so "npm ci ..." in the gate skips the npm marker without
    # touching the python one, and vice versa.
    SKIP_HINTS = {
        "package.json": (r"\bnpm\s+(?:ci|install)\b",),
        # Only skip the venv recipe when the gate itself *creates* the venv. Merely
        # referencing .venv/bin/python in the gate is a consumer, not an installer —
        # dj-bot's gate runs `.venv/bin/python -m pytest` yet has no .venv in a fresh
        # worktree, and skipping here is exactly the red-baseline bug this fixes.
        "requirements.txt": (r"\bpython3?\s+-m\s+venv\b",),
        "pyproject.toml": (r"\bpython3?\s+-m\s+venv\b",),
        "Pipfile": (r"\bpython3?\s+-m\s+venv\b",),
        "Gemfile": (r"\bbundle\s+install\b",),
        "go.mod": (r"\bgo\s+mod\s+download\b",),
        "Cargo.toml": (r"\bcargo\s+(?:build|fetch)\b",),
        "build.gradle": (r"\b(?:mvn|gradle|gradlew)\b",),
        "pom.xml": (r"\bmvn\b",),
        "composer.json": (r"\bcomposer\s+install\b",),
        "mix.exs": (r"\bmix\s+deps\.get\b",),
    }
    hooks, skipped = [], []
    for markers, recipe_spec in PROV_RECIPES:
        marker = next((m for m in markers if s["runtime"].get(m)), None)
        if marker is None:
            continue
        hints = SKIP_HINTS.get(marker, ())
        if any(re.search(pattern, test_cmd) for pattern in hints):
            skipped.append(marker)
            continue
        recipe = recipe_spec[marker] if isinstance(recipe_spec, dict) else recipe_spec
        # A marker found one level down (dj-bot keeps spike/package.json + spike/node_modules)
        # needs the recipe to run inside that subdirectory, or npm ci at the root has no
        # manifest to install. Default is the repo root ("cd . && ..." is a no-op).
        sub = s.get("subdirs", {}).get(marker, ".")
        if sub == ".":
            hooks.append(recipe)
        else:
            hooks.append(f"cd {shlex.quote(sub)} && {recipe}")
        skipped.append(marker)
    for name in s.get("nested", []):
        source = s.get("nested_sources", {}).get(name, name)
        hooks.append(f"git clone --quiet {shlex.quote(source)} {shlex.quote('./' + name)}")
        skipped.append(f"nested {name}")
    return {"hooks": hooks, "env": {}, "timeout": 1800, "_skipped": skipped}


def render_surroundings(repo, slug, s, config_path, existing=None, git_branch=None):
    """A human-readable map of the repo's surroundings, to a string."""
    if git_branch is None:
        git_branch = _default_git_branch
    lines = [f"repo:            {repo} ({git_branch(repo)})",
             f"config:          {config_path}"]
    if s.get("nested"):
        lines.append(f"nested repos:    {', '.join(s['nested'])}  (absent from every worktree)")
    runtime = [k for k, v in s.get("runtime", {}).items() if v]
    lines.append("runtime deps:    " + (", ".join(runtime) if runtime else "none detected"))
    if s.get("venv"):
        lines.append("venv:            present (.venv)")
    if s.get("node_modules"):
        lines.append("node_modules:    present (not in git — fresh worktrees lack it)")
    for marker, sub in sorted((s.get("subdirs") or {}).items()):
        lines.append(f"{marker}:        under {sub}/ — installs must cd there (prov handles this)")
    if s.get("gitignore"):
        lines.append("gitignored:      " + ", ".join(sorted(s["gitignore"])[:8]))
    if existing:
        prov = existing.get("prov") or {}
        lines.append(f"prov hooks:      {len(prov.get('hooks', []))} configured")
        if prov.get("hooks"):
            lines.append("  (re-run `swarm init <repo>` to refresh)")
    return "\n".join(lines)



