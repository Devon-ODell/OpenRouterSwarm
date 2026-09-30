#!/usr/bin/env python3
"""Undo for flint turns (Phase 2, albatross parity).

Albatross keeps a per-session transcript it can replay; this module keeps the same
information with far less machinery: before a tool call changes the working tree,
the tool layer snapshots the affected file into a session-local undo log, and
`flint --undo <session-id>` (or the panel's Undo command) restores it.

The log is one JSONL file per session in ~/.flint/undo/ (0600), each line
`{path, before, after, at, command}`. Restoring replays the entries in reverse and
only rewrites a file when its current content still matches the recorded `after`
state — so an undo never clobbers an edit the user kept since the turn.

No network calls; the session directory is under FLINT_HOME, not the checkout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def session_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("FLINT_HOME", "~/.flint")).expanduser() / "undo"


def _read_bytes(p):
    try:
        return Path(p).expanduser().read_bytes()
    except OSError:
        return None


def snapshot(session_id, path, after=None, command="", env=None):
    """Record the `before` state of one file, once per session per path.

    `after` is the content about to be written; it becomes the clobber guard on
    undo. Returns True when a snapshot was recorded.
    """
    p = Path(path).expanduser()
    before = _read_bytes(p)
    if before is None:
        return False
    _append(session_id, {
        "path": str(p),
        "before": before.decode("utf-8", "replace"),
        "after": after if isinstance(after, str) else (after.decode("utf-8", "replace") if after else None),
        "command": command,
    }, env=env)
    return True


def _append(session_id, entry, env=None):
    d = session_dir(env)
    entry = dict(entry, at=time.time(), session=session_id)
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = d / f"{session_id}.jsonl"
        with open(log, "a") as f:
            f.write(json.dumps(entry) + "\n")
        try:
            os.chmod(log, 0o600)
        except OSError:
            pass
    except OSError:
        pass  # undo is a convenience, never fatal to the turn


def entries(session_id, env=None):
    env = os.environ if env is None else env
    path = session_dir(env) / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def list_sessions(env=None, limit=50):
    """Recent sessions that have an undo log, newest first: [{session, files, at}]."""
    env = os.environ if env is None else env
    d = session_dir(env)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        rows = entries(p.stem, env)
        if not rows:
            continue
        out.append({"session": p.stem, "files": len({r.get("path") for r in rows}),
                    "at": max(r.get("at") or 0 for r in rows), "path": str(p)})
        if len(out) >= limit:
            break
    return out


def undo(session_id, env=None, dry_run=False):
    """Restore every snapshot in one session, reverse order. Returns (restored, skipped).

    A file whose current content does not match its recorded `after` state is
    skipped: something has changed it since the turn, and undo must not clobber
    that. Files the turn created (no `before`) are skipped too — there is no
    pre-state to restore, removal would be guesswork.
    """
    rows = entries(session_id, env)
    restored, skipped = [], []
    seen = set()
    for row in reversed(rows):
        p = Path(row.get("path") or "")
        if str(p) in seen:
            continue  # later rows for the same path supersede earlier ones
        seen.add(str(p))
        before, after = row.get("before"), row.get("after")
        if before is None:
            continue  # created this session: nothing to restore
        current = _read_bytes(p)
        if current is None:
            skipped.append((str(p), "missing"))
            continue
        if after is not None:
            current_txt = current.decode("utf-8", "replace")
            after_txt = after if isinstance(after, str) else after.decode("utf-8", "replace")
            if current_txt != after_txt:
                skipped.append((str(p), "changed since the turn"))
                continue
        try:
            if not dry_run:
                p.write_bytes(str(before).encode("utf-8"))
            restored.append((str(p), len(str(before))))
        except OSError as e:
            skipped.append((str(p), str(e)))
    return restored, skipped