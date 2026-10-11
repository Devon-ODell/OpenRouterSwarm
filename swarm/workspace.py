"""Worktree, git and trunk primitives extracted from the daemon.

This module deliberately has no dependency on the daemon.  Every global the
extracted bodies used is injected as an argument by the daemon wrappers.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path


def trunk_name(c):
    return c.get("trunk", "swarm/trunk")


def sh(cmd, cwd=None, timeout=900, env=None, *, process, kill_group):
    with process(cmd, cwd=cwd, shell=isinstance(cmd, str),
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, env=env) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except BaseException:
            kill_group(p)
            p.communicate()
            raise
    return p.returncode, (out or "") + (err or "")


def git(args, cwd, check=False, *, sh, ident):
    # Agents can write inside worktrees; never let that reach hooks the supervisor runs.
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    rc, out = sh(cmd, cwd=cwd, timeout=180, env={**os.environ, **ident})
    if check and rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out[-500:]}")
    return rc, out.strip()


@contextmanager
def trunk_locked(*, thread_lock, state):
    with thread_lock, open(state / "trunk.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def ensure_trunk(c, *, git, locked, log):
    t = trunk_name(c)
    with locked():
        rc, _ = git(["rev-parse", "--verify", "-q", f"refs/heads/{t}"], cwd=c["repo"])
        if rc != 0:
            git(["branch", t, c.get("base_branch", "main")], cwd=c["repo"], check=True)
            log(f"created {t} from {c.get('base_branch', 'main')}")


def checked_out_at(c, branch, *, git):
    _, out = git(["worktree", "list", "--porcelain"], cwd=c["repo"])
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def refresh_view(c, *, git, ensure_trunk, worktrees):
    """A detached, read-only checkout of trunk for the planner and decomposer."""
    ensure_trunk(c)
    view = worktrees() / "_view"
    if (view / ".git").exists():
        git(["checkout", "-q", "--detach", "-f", trunk_name(c)], cwd=view, check=True)
        git(["clean", "-fdq"], cwd=view)
    else:
        git(["worktree", "prune"], cwd=c["repo"])
        view.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "--detach", str(view), trunk_name(c)], cwd=c["repo"], check=True)
    return view


def sync_refresh_view(c, *, refresh_view):
    """The reader for sync_trunk; never needs provisioning (deps cannot affect a merge)."""
    return refresh_view(c)


def retained_diff(c, attempt, *, git):
    """The work an interrupted attempt left behind, as a patch, or "".

    Two places it can be. _cleanup commits unaccepted work to the attempt's branch, so usually
    it is a commit; when the daemon was killed before cleanup ran it is still sitting in the
    worktree. Both are read the same way, against the commit the attempt started from."""
    base, branch = attempt.get("base_commit"), attempt.get("branch")
    if not base or not branch:
        return ""
    # git() strips its output, and `git apply` rejects a patch with no trailing newline.
    if git(["rev-parse", "--verify", "-q", f"{branch}^{{commit}}"], cwd=c["repo"])[0] == 0:
        rc, diff = git(["diff", "--unified=3", base, branch], cwd=c["repo"])
        if rc == 0 and diff.strip():
            return diff + "\n"
    wt = Path(attempt.get("worktree") or "")
    if wt.is_dir() and (wt / ".git").exists():
        git(["add", "-A"], cwd=wt)
        rc, diff = git(["diff", "--cached", "--unified=3", base], cwd=wt)
        if rc == 0 and diff.strip():
            return diff + "\n"
    return ""


def refresh_view_prov(c, *, refresh_view, prov_block, run_prov_hooks, log):
    """The fully-provisioned _view: the bare checkout plus its provisioning hooks, with a
    note when a hook changed files so the planner's first look at the tree is accurate.
    """
    view = refresh_view(c)
    if prov_block(c)["hooks"]:
        ok, out = run_prov_hooks(c, view, "view")
        if not ok:
            log(f"_view provisioning failed:\n{out}")
    return view


def sync_trunk(c, *, git, ensure_trunk, locked, worktrees, run_gate_with_prov,
               checked_out_at, log, sync_failed):
    """Bring new commits from base_branch into trunk when they merge cleanly and stay green."""
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    ensure_trunk(c)
    rc, _ = git(["merge-base", "--is-ancestor", base, t], cwd=repo)
    if rc == 0:
        return "current"
    _, base_sha = git(["rev-parse", base], cwd=repo)
    if sync_failed.get(base) == base_sha:
        return "skipped"
    with locked():
        _, old = git(["rev-parse", t], cwd=repo, check=True)
        tmp = worktrees() / f"_sync-{uuid.uuid4().hex[:6]}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "--detach", str(tmp), old], cwd=repo, check=True)
        try:
            rc, out = git(["merge", "--no-edit", "-m", f"swarm: sync {base} into {t}", base], cwd=tmp)
            if rc != 0:
                git(["merge", "--abort"], cwd=tmp)
                log(f"{base} has changes that conflict with {t}; continuing on {t} without them")
                sync_failed[base] = base_sha
                return "conflict"
            # The sync worktree is just as bare as an attempt worktree.  Reuse the same
            # provisioning path or repositories whose gate consumes ignored dependencies
            # (node_modules, .venv, nested repositories) can never absorb upstream commits.
            ok, output = run_gate_with_prov(c, tmp, "sync")
            if not ok:
                log(f"merging {base} into {t} fails the tests; continuing without it")
                sync_failed[base] = base_sha
                return "red"
            if checked_out_at(c, t):
                return "checked-out"
            _, new = git(["rev-parse", "HEAD"], cwd=tmp, check=True)
            git(["update-ref", f"refs/heads/{t}", new, old], cwd=repo, check=True)
            log(f"synced new {base} commits into {t}")
            return "synced"
        finally:
            git(["worktree", "remove", "--force", str(tmp)], cwd=repo)


def create_worktree(c, task_id, *, git, ensure_trunk, worktrees):
    # Every attempt gets its own branch from trunk: never reset a user's
    # checkout or discard a previous attempt's changes.
    ensure_trunk(c)
    name = f"{task_id}-{uuid.uuid4().hex[:8]}"
    branch = f"swarm/{name}"
    wt = worktrees() / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(wt), trunk_name(c)],
        cwd=c["repo"], check=True)
    return branch, wt


def adopt_retained(task, wd, *, take_resume, git, log, journal, worker):
    """Re-apply work an interrupted attempt left behind, or None.

    The patch is applied to a worktree freshly branched from trunk rather than the old
    worktree being reused: trunk has moved on, and a stale checkout is a worse starting
    point than none. If it no longer applies, the attempt simply proceeds as normal — the
    model writes it again, which is exactly what used to happen every time."""
    row = take_resume(task["id"])
    if not row:
        return None
    patch = Path(row.get("patch") or "")
    if not patch.is_file():
        return None
    rc, out = git(["apply", "--index", "--whitespace=nowarn", str(patch)], cwd=wd)
    if rc != 0:
        log(f"{task['id']}: the retained work no longer applies to trunk; "
            f"implementing it again ({out.strip()[:160]})", worker)
        journal("resume_failed", id=task["id"], attempt=row.get("attempt"),
                reason=out.strip()[:300])
        git(["reset", "--hard"], cwd=wd)     # leave the worktree exactly as it was
        return None
    log(f"{task['id']}: re-applied {row.get('lines', '?')} lines from the interrupted "
        f"attempt {row.get('attempt')}; verifying rather than rewriting", worker)
    journal("resumed", id=task["id"], attempt=row.get("attempt"),
            implementer=row.get("implementer"), lines=row.get("lines"))
    return row


def owns_branch(wt, branch, *, git):
    rc, ref = git(["symbolic-ref", "-q", "HEAD"], cwd=wt)
    return rc == 0 and ref == f"refs/heads/{branch}"


def snapshot(wt, branch, review_base, *, git, owns_branch, error):
    if not owns_branch():
        raise error(f"worker switched away from {branch}")
    git(["add", "-A"], cwd=wt, check=True)
    _, tree = git(["write-tree"], cwd=wt, check=True)
    _, diff = git(["diff", "--cached", "--unified=3", review_base], cwd=wt, check=True)
    return tree, diff


def cleanup(c, wt, branch, stage, task, *, git, owns_branch):
    """Keep every attempt's work on its branch; remove the worktree if configured."""
    if not wt or not Path(wt).exists():
        return
    repo, t = c["repo"], trunk_name(c)
    if stage != "accepted" and owns_branch():
        git(["add", "-A"], cwd=wt)
        rc, _ = git(["diff", "--cached", "--quiet"], cwd=wt)
        if rc == 1:
            git(["commit", "-q", "--no-verify", "-m",
                 f"swarm (not accepted: {stage}): {task['title']}"], cwd=wt)
    if c.get("keep_worktrees", True):
        return
    git(["worktree", "remove", "--force", str(wt)], cwd=repo)
    _, ahead = git(["rev-list", "--count", f"{t}..{branch}"], cwd=repo)
    if ahead == "0":  # landed on trunk, or never produced anything
        git(["branch", "-D", branch], cwd=repo)