#!/usr/bin/env python3
"""Write-restricting macOS sandbox for unattended agents and the tests they write.

Inside it a process may write only to its worktree, that worktree's Git
metadata and the shared object store, FLINT_HOME, temp and package caches.
Reads of common credential stores and .env files are denied. Network stays
open because models and package registries need it, so this contains accidents
and careless file access; it is not a boundary against a determined attacker.
"""
import functools
import os
import subprocess
import sys
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
CACHES = ["~/Library/Caches", "~/.cache", "~/.npm", "~/.yarn", "~/.pnpm-store",
          "~/Library/pnpm", "~/go/pkg", "~/.cargo/registry", "~/.cargo/git",
          "~/.gradle", "~/.m2", "~/.rustup/tmp"]
SECRETS = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.kube",
           "~/.docker/config.json", "~/.config/gcloud", "~/.azure", "~/.pypirc"]
ENV_FILES = r"/\.env(\.(bak|local|prod|production|dev|development))?$"


def _real(p):
    return os.path.realpath(os.path.expanduser(str(p)))


def _q(p):
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


@functools.lru_cache(maxsize=1)
def available():
    """True when sandbox-exec exists and can apply a profile here (it cannot nest)."""
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return False
    try:
        r = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def profile(writable, extra_write=()):
    paths = [_real(p) for p in (*writable, *extra_write, "/private/tmp", "/private/var/folders",
                                *CACHES) if p]
    allow = " ".join(f"(subpath {_q(p)})" for p in dict.fromkeys(paths))
    deny = " ".join(f"(subpath {_q(_real(p))})" for p in SECRETS)
    # Later rules win: default allow, then no writes, then the write allowlist,
    # then secret reads denied last so no allowance can reopen them.
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath \"/dev\") {allow})",
        f"(deny file-read* file-write* {deny} (regex #\"{ENV_FILES}\"))",
    ])


def wrap(cmd, prof):
    """Prefix a command (argv list, or shell string) with sandbox-exec."""
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]
    return [SANDBOX_EXEC, "-p", prof, *cmd]


def git_paths(worktree):
    """The worktree's private Git dir and the shared object store it writes into."""
    out = []
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            r = subprocess.run(["git", "rev-parse", flag], cwd=worktree, capture_output=True,
                               text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        p = Path(r.stdout.strip())
        out.append(p if p.is_absolute() else Path(worktree) / p)
    gitdir, common = out
    return [gitdir, common / "objects"]
