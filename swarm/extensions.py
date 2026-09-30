#!/usr/bin/env python3
"""Extensions runtime (Phase 3, P2).

Albatross and Cursor both support hooking into the harness; the change request asks
for an extensions runtime with the same trust posture albatross adopted: a
config-hash allowlist, never auto-spawning changed servers. This module is that
runtime in its smallest useful form.

An extension is a directory under ~/.flint/extensions/ containing:

    extension.json     {"name": "...", "command": ["python3", "tool.py"], "events": ["PostToolUse"]}
    ...                the actual script

The first time an extension is seen, the bridge records a sha256 of its
`extension.json` + script files in ~/.flint/trusted.json. Any later run whose hash
changed (or which has no trust record) is refused unless the caller passes
`--trust-extension <name>` explicitly. That is the "never run changed servers
automatically" rule: a malicious edit to an installed extension is inert until the
user trusts the new version.

Events are the same names flint's hooks already use (PostToolUse, TurnComplete,
TaskDone); an extension is run with the event name as $1 and a JSON payload on
stdin, same contract as swarm/hooks.py. Extensions are for people who want more
than a one-line hook command and need a real script.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

TRUST_FILE = "trusted.json"


def ext_root(env=None):
    env = os.environ if env is None else env
    return Path(env.get("FLINT_HOME", "~/.flint")).expanduser() / "extensions"


def _trust_path(env=None):
    return ext_root(env).parent / TRUST_FILE


def _hash_of(entry, env=None):
    """sha256 over the manifest and every file it names (sorted, for stability)."""
    h = hashlib.sha256()
    h.update(json.dumps(entry.get("command"), sort_keys=True).encode())
    h.update(json.dumps(entry.get("events") or [], sort_keys=True).encode())
    base = ext_root(env) / entry.get("name", "?")
    for rel in sorted(entry.get("files") or []):
        p = base / rel
        if p.is_file():
            h.update(rel.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _read_manifest(env=None):
    root = ext_root(env)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        mf = d / "extension.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("command"), list):
            continue
        data.setdefault("name", d.name)
        data.setdefault("events", [])
        data.setdefault("files", sorted(p.name for p in d.iterdir() if p.is_file() and p.name != "extension.json"))
        data["_hash"] = _hash_of(data, env)
        out.append(data)
    return out


def _trusted(env=None):
    """{extension_name: trusted_hash} from ~/.flint/trusted.json."""
    try:
        store = json.loads(_trust_path(env).read_text())
        exts = store.get("extensions") if isinstance(store, dict) else None
        return {k: str(v) for k, v in (exts or {}).items()} if isinstance(exts, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _trust(name, data, env=None):
    path = _trust_path(env)
    try:
        store = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        store = {}
    if not isinstance(store, dict):
        store = {}
    exts = store.setdefault("extensions", {})
    exts[name] = data.get("_hash", "")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(path)
    return path


def trust(name, env=None):
    """Trust an extension by name (records its current hash)."""
    for m in _read_manifest(env):
        if m.get("name") == name or m.get("name") == name.replace("_", "-"):
            return str(_trust(m["name"], m, env))
    return None


def run_extensions(events, cwd, payload=None, allow_untrusted=False, env=None):
    """Run every extension registered for `events`, honouring the trust allowlist.

    Returns a list of results, one per extension invocation. An untrusted (or
    changed) extension is reported as `trusted: false` and skipped unless
    `allow_untrusted` is passed — tests use that to exercise the runner without
    a pre-trust step; the bridge always resolves trust first.
    """
    env = os.environ if env is None else env
    trusted = _trusted(env)
    results = []
    for m in _read_manifest(env):
        if not any(e in (events or []) for e in m.get("events") or []):
            continue
        ok_trust = trusted.get(m["name"]) == m.get("_hash")
        if not ok_trust and not allow_untrusted:
            results.append({"name": m["name"], "event": events,
                            "ok": False, "trusted": False,
                            "error": "not trusted; run bridge.py extension --trust <name>"})
            continue
        try:
            p = subprocess.run(
                list(m["command"]) + events,
                input=json.dumps(payload or {}),
                capture_output=True, text=True, timeout=15,
                cwd=str(ext_root(env) / m["name"]),   # the extension runs in its own folder
                env=dict(os.environ, FLINT_EXTENSION=m["name"], **(env or {})),
            )
            results.append({"name": m["name"], "event": events, "ok": p.returncode == 0,
                            "trusted": True, "exit": p.returncode,
                            "output": (p.stdout or "")[:1000]})
        except subprocess.TimeoutExpired:
            results.append({"name": m["name"], "event": events, "ok": False,
                            "trusted": True, "error": "timed out after 15s"})
        except OSError as e:
            results.append({"name": m["name"], "event": events, "ok": False,
                            "trusted": True, "error": str(e)})
    return results