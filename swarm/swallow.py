"""A failure-proof record of exceptions the harness chose to swallow.

The daemon already journals task failures richly (`journal("error", ...)`); this module
catches the *other* class: the quiet fallbacks in support modules — a probe that failed, a
catalog that was unreachable, an import path that fell back — where the code deliberately
returns a default instead of raising. Those are the ones that make a machine "work
yesterday and not today" with nothing in any log to say why.

Design rules:
- Importable both as `swarm.swallow` and as bare `swallow` (support modules use both styles).
- Never raises. Recording must not change the behavior of the code that swallowed.
- Thread-safe (the bridge and the daemon are multi-threaded).
- Bounded: keeps the most recent RECORD_LIMIT entries in one JSONL file under the daemon's
  state dir, rotated by rewriting the tail. The file is replaced atomically.
- Site is `module:function` so `swarm exceptions` can group by it without stack walking.

The `swarm exceptions` command and `swarm status`'s summary line read this file.
"""

import json
import os
import threading
import time

try:
    from pathlib import Path
    _STATE = Path(os.environ.get("FLINT_STATE_DIR", "")) if os.environ.get("FLINT_STATE_DIR") else None
except Exception:  # pragma: no cover - environment is broken; never let this raise
    _STATE = None

RECORD_LIMIT = 200
_lock = threading.Lock()
_last_error_logged = 0.0


def _state_dir():
    """The daemon's state dir, best-effort. Callers never depend on this succeeding."""
    if _STATE is not None:
        return _STATE
    # Match swarmd's STATE resolution: the repo's swarm/state, or ~/.flint for standalone flint.
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(os.path.dirname(here), "swarm", "state")
    if os.path.isdir(candidate):
        return Path(candidate)
    home = os.path.expanduser("~/.flint")
    return Path(home) if os.path.isdir(home) else Path(candidate)


def _path():
    return _state_dir() / "swallowed.jsonl"


def record(site, exc, note=""):
    """Record one swallowed exception. Never raises; never blocks the caller on I/O for long."""
    try:
        row = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "site": str(site)[:160],
            "kind": type(exc).__name__,
            "msg": str(exc)[:300],
            "note": str(note)[:200],
        }
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            rows = []
            if p.exists():
                try:
                    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
                except (OSError, ValueError):
                    rows = []
            rows.append(row)
            rows = rows[-RECORD_LIMIT:]
            tmp = p.with_suffix(".tmp")
            tmp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            os.replace(tmp, p)  # atomic; a crash mid-write never corrupts the file
    except Exception:
        # The recorder must be invisible: if state is unwritable (read-only fs, bad env),
        # the swallow site behaves exactly as it did before this module existed.
        pass


def recent(limit=50):
    """Newest-first swallowed exceptions, or [] when none/unreadable."""
    try:
        p = _path()
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        return rows[-limit:][::-1]
    except Exception:
        return []


def summary():
    """Counts grouped by site, newest-first, for the status line: [{site, count, last_iso, last_msg}]."""
    rows = recent(1000)
    by_site = {}
    for r in rows:
        s = by_site.setdefault(r.get("site", "?"), {"site": r.get("site", "?"), "count": 0,
                                                    "last_iso": None, "last_msg": ""})
        s["count"] += 1
        if s["last_iso"] is None:
            s["last_iso"] = r.get("iso")
            s["last_msg"] = r.get("msg", "")
    return list(by_site.values())