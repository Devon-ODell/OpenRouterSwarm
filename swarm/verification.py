"""Verification helpers: weakened-test detection, failure output reading and gate execution.

Extracted from the daemon (P2, P7). Pure functions of their parameters; no module reads
daemon globals.
"""
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

ASSERT_CALL = re.compile(
    r"\b(assert\w*(?:\.\w+)*|expect|require|should\w*|t\.(?:Error|Fatal)f?)\b")
# Directory names too common to identify anything: "games" is not what "games/reversi" means.
GENERIC_DIR = frozenset({"", ".", "/", "src", "lib", "app", "test", "tests", "spec", "specs",
                         "games", "site", "docs", "scripts", "static", "public", "assets"})


def assert_counts(lines):
    """How many times each assertion function is called across these lines."""
    counts = {}
    for line in lines:
        for m in ASSERT_CALL.finditer(line):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def identifiers(path):
    """The names that stand for this path in code: the path, its directory, and their names."""
    p = Path(path)
    out = {str(path), p.name, p.stem}
    parent = p.parent
    if str(parent) not in GENERIC_DIR:
        out |= {str(parent), parent.name}
    return {x for x in out if x and x not in GENERIC_DIR}


def deleted_paths(diff):
    """Every name that identifies something this diff deletes."""
    out, pending = set(), None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            pending = line.split(" b/", 1)[-1].strip()
        elif line.startswith("deleted file mode") and pending:
            out |= identifiers(pending)
            pending = None
    return out


def test_hunks(diff):
    """[(path, deleted, [hunk lines])] for the test files this diff touches."""
    files, path, deleted, is_test, hunks = [], None, False, False, None

    def close():
        if is_test and path:
            files.append((path, deleted, hunks))
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            close()
            path = line.split(" b/", 1)[-1].strip()
            low = path.lower()
            is_test = any(m in low for m in ("test", "spec_", "_spec"))
            deleted, hunks = False, []
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("@@") and is_test:
            hunks.append([])
        elif is_test and hunks and line[:1] in ("+", "-", " "):
            hunks[-1].append(line)
    close()
    return files


def names_deleted(line, gone):
    """True when this line refers to something the diff deletes.

    A test that loads games/star-catcher/game.js writes it as
    path.join(__dirname, '..', 'games', 'star-catcher', 'game.js'), so the slug is what can be
    matched, not the joined path."""
    return any(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line) for name in gone)


def names_its_source(test_path, gone):
    """True when a deleted test file is named after something else the diff deletes.

    tests/test_reversi.py deleted alongside games/reversi/ is one removal, not a weakening;
    tests/test_core.py deleted while the code stays is the clearest weakening there is."""
    stem = Path(test_path).stem.lower()
    return any(len(name) >= 3 and name.lower() in stem for name in gone)


def weakened_tests(diff, allow_test_changes=False):
    """Assertions this diff took out of test files that are still meant to exist.

    The old check counted every removed line containing "assert" under any path containing
    "test". All four hits across 189 studio attempts were legitimate, and this check carries the
    heaviest penalty in the ledger (weight 6) and hangs the model on the rafters, so a false
    positive punishes a model for doing what the owner asked:

      - two attempts deleted games/reversi/ and games/star-catcher/ and the assertions that
        loaded them, which is what "remove star catcher and reversi" means;
      - one was the owner's own removal request, again;
      - one changed `assert.equal(b[112], 3)` to `assert.equal(b[80], 3)` — an assertion edited
        to match an 8x15 board, not an assertion removed.

    So: count per assertion function, treat a removal paired with an addition of the same
    function in the same hunk as an edit, skip hunks that remove code for files the diff deletes,
    and skip a test file deleted alongside the source it tested.
    """
    if allow_test_changes:
        return 0                       # the owner authorised this; the adversary is told so
    total, all_gone = 0, deleted_paths(diff)
    for path, deleted, hunks in test_hunks(diff):
        # A file cannot be its own deleted source, or every deleted test file would excuse itself.
        gone = all_gone - identifiers(path)
        if deleted:
            if names_its_source(path, gone):
                continue               # the test went with the code it tested
            # A test file deleted while the code it tests stays is the plainest weakening there
            # is, so every assertion it took with it counts.
            for lines in hunks:
                total += sum(assert_counts([l[1:] for l in lines if l.startswith("-")]).values())
            continue
        for lines in hunks:
            minus = [l[1:] for l in lines if l.startswith("-")]
            plus = [l[1:] for l in lines if l.startswith("+")]
            if gone and any(names_deleted(l, gone) for l in minus):
                continue               # this hunk removes the use of something that is gone
            back = assert_counts(plus)
            for name, n in assert_counts(minus).items():
                total += max(0, n - back.get(name, 0))
    return total


def last_error(output):
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    for l in reversed(lines):
        if re.search(r"error|fail|assert|exception|panic", l, re.I):
            return l[:200]
    return lines[-1][:200] if lines else "no output"


# ------------------------------------------------------------------ gate

def baseline_key(c, commit):
    """What a cached baseline pass is a pass *of*: this commit and these exact commands."""
    parts = [str(commit), c.get("test_cmd", ""), *(c.get("validation_commands") or [])]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def baseline_cache(key, passed=None, ttl=86_400, *, state):
    """Read or record that trunk at this commit passed its own gate.

    Every attempt ran the full suite before the model started, and the studio's suite takes 105
    seconds. Nothing about trunk changed between one attempt and the next, so most of those runs
    proved the same thing over again.
    """
    path = state / "baseline.json"
    if passed is None:
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if row.get("key") != key or not row.get("passed"):
            return None
        return row if time.time() - row.get("at", 0) < ttl else None
    row = {"key": key, "passed": bool(passed), "at": time.time()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(path)
    return row


def run_gate(cwd, c, *, sandboxed, sh, clean_env):
    cmd = sandboxed(c["test_cmd"], cwd, c)
    rc, out = sh(cmd, cwd=cwd, timeout=c.get("test_timeout", 900), env=clean_env())
    if c.get("_gate_log"):
        Path(c["_gate_log"]).write_text(f"command: {c['test_cmd']}\nexit: {rc}\n{out}")
    return rc == 0, out[-4000:]


# ------------------------------------------------------------------ provisioning
#
# A fresh git worktree contains exactly the files git tracks: no node_modules/, no .venv,
# no vendored deps, no nested-repo content. A test gate that needs any of those fails on
# a bare checkout before a single model request, which is how the dj-bot swarm spent its
# whole shift red-baselined with "Cannot find package '@strudel/core'" and "every task
# would be rejected". The `prov` config block fixes that: it is a list of shell hooks the
# swarm runs inside each fresh worktree (and the _view) before the gate, so a checkout in
# a new codespace or on a new laptop reaches green by itself.


def prov_block(c):
    """The provisioning section of the live config, normalised."""
    p = c.get("prov") or {}
    if isinstance(p, list):
        p = {"hooks": [h for h in p if isinstance(h, str)]}
    elif not isinstance(p, Mapping):
        return {"hooks": [], "env": {}, "timeout": 1200}
    hooks = [h for h in p.get("hooks") or [] if isinstance(h, str) and h.strip()]
    env = {k: str(v) for k, v in (p.get("env") or {}).items() if v is not None}
    return {"hooks": hooks, "env": env, "timeout": int(p.get("timeout", 1200) or 1200)}


def run_prov_hooks(c, wd, label, env_extra=None, log_on_ok=False, *,
                   prov_block, sh, clean_env, log):
    """Run every provisioning hook in `wd` (a fresh worktree or the _view).

    Hooks are allowed to write anywhere: provisioning must install packages, which the
    sandbox would refuse, so it runs unsandboxed. The only thing we guarantee is that a
    hook that fails stops the attempt with its output attached (so the agent never sees a
    red gate it cannot explain) and that hook output is attributed to `label` in the log.
    Returns (ok, output). Never raises for a failing hook.
    """
    prov = prov_block(c)
    if not prov["hooks"]:
        return True, ""
    # Package-manager installs don't need the daemon's own secrets (OPENROUTER_API_KEY and
    # friends); clean_env() is what run_gate() already uses for the same reason.
    env = {**clean_env(), **prov["env"]}
    if env_extra:
        env.update(env_extra)
    out = []
    for i, hook in enumerate(prov["hooks"], 1):
        log(f"prov [{label}] hook {i}/{len(prov['hooks'])}: {hook[:140]}")
        try:
            rc, o = sh(hook, cwd=str(wd), timeout=prov["timeout"], env=env)
        except Exception as e:
            out.append(f"[hook {i}] {hook[:140]}\n{type(e).__name__}: {e}")
            log(f"prov [{label}] hook {i} raised {type(e).__name__}: {e}")
            return False, "\n".join(out)[-4000:]
        out.append(f"[hook {i}] exit {rc}: {hook[:140]}\n{o}".strip())
        if rc != 0:
            log(f"prov [{label}] hook {i} failed (exit {rc})")
            return False, "\n".join(out)[-4000:]
        if log_on_ok:
            log(f"prov [{label}] hook {i} ok")
    return True, "\n".join(out)[-4000:]


def run_gate_with_prov(c, wd, label, *, run_prov_hooks, run_gate):
    """Provision `wd`, then run its gate. Packaging deps into every worktree is exactly
    what a pre-flight baseline is for, so this is the one gate runner that provisions
    first. Returns (ok, output) like run_gate. If there is nothing to provision this is
    exactly run_gate — the gate itself is what decides whether `wd` is real."""
    ok, out = run_prov_hooks(c, wd, label, log_on_ok=True)
    if not ok:
        return False, f"provisioning failed:\n{out}"
    return run_gate(wd, c)


def run_gate_commands(c, wd, commands, label, gate_no, evidence_dir, provisioned, *,
                      run_gate, run_prov_hooks, mark_progress):
    """Run configured commands in `wd` and retain supervisor-owned evidence.

    Provisions once before the first command, keeps the dependencies across candidate and
    repair gates, and returns (evidence, passed, provisioned)."""
    evidence, passed = [], True
    for i, command in enumerate(commands):
        name = f"gate-{gate_no:02d}-{label}-{i}.log"
        config = dict(c, test_cmd=command, _gate_log=str(evidence_dir / name))
        started = time.monotonic()
        try:
            if i == 0 and not provisioned:
                # Every attempt begins in a bare worktree. Provision it once, before the
                # baseline, then retain those dependencies through candidate/repair gates.
                prov_ok, prov_output = run_prov_hooks(
                    c, wd, "attempt", log_on_ok=True)
                if not prov_ok:
                    ok, output = False, f"provisioning failed:\n{prov_output}"
                else:
                    provisioned = True
                    ok, output = run_gate(wd, config)
            else:
                ok, output = run_gate(wd, config)
        except subprocess.TimeoutExpired:
            ok, output = False, "verification command timed out"
            Path(config["_gate_log"]).write_text(f"command: {command}\n{output}\n")
        row = {"command": command, "passed": ok, "log": name,
               "seconds": round(time.monotonic() - started, 3), "tail": output}
        evidence.append(row)
        mark_progress("verification completed")
        passed = passed and ok
    return evidence, passed, provisioned


