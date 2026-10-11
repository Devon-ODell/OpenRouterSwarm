"""Worktree, git and trunk primitives extracted from the daemon.

This module deliberately has no dependency on the daemon.  Every global the
extracted bodies used is injected as an argument by the daemon wrappers.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager


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