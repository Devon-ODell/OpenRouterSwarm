#!/usr/bin/env python3
"""Hooks / extension API (Phase 2, albatross `hooks:` parity).

agent.config.json may carry a `hooks` block naming commands to run at lifecycle
points:

    "hooks": {
      "PostToolUse": [ { "hooks": [{ "type": "command", "command": "$HOME/bin/notify" }] } ]
    }

A hook is run with the event name as `$1` and a JSON summary on stdin, with a hard
timeout so a slow or hung hook can never stall a turn. The command is resolved
against $HOME and the repo root, and only runs when it exists on disk — a hook
that silently vanished must not break the edit that triggered it (albatross's
trust posture: never spawn changed servers automatically; same rule here for
spawned commands).

Events flint raises: PreToolUse, PostToolUse, TurnComplete, TaskDone.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

HOOK_TIMEOUT = 10.0


def _resolve(command, cwd):
    command = str(command or "").strip()
    if not command:
        return None
    # $HOME at the front is the common shape; expand it by hand so a missing
    # binary is a clean None rather than a shell error deep inside a turn.
    for prefix, repl in (("$HOME/", os.path.expanduser("~/")),
                         ("~", os.path.expanduser("~") + os.sep)):
        if command.startswith(prefix):
            command = repl + command[len(prefix):]
            break
    cmd = shlex.split(command)
    if not cmd:
        return None
    exe = Path(cmd[0])
    if not exe.is_absolute():
        exe = Path(cwd) / cmd[0]
    if not exe.is_file() or not os.access(exe, os.X_OK):
        # Fall back to PATH lookups for bare names like `notify`.
        found = shutil_which(cmd[0])
        if found is None:
            return None
        cmd[0] = found
        return cmd
    cmd[0] = str(exe)
    return cmd


def shutil_which(name):
    """shutil.which, imported lazily (avoids an import at module load)."""
    import shutil
    return shutil.which(name)


def run_hooks(events, cwd, payload=None, env=None):
    """Run the hooks registered for `events` (a list of names) and return their output.

    `payload` is a dict serialized to JSON on the hook's stdin. Every hook output
    is captured and truncated; a hook that times out or fails is reported, never
    raised — the edit it belongs to has already happened.
    """
    config = _hooks_config(env=env)
    results = []
    for event in events or []:
        for block in config.get(event) or []:
            for hook in block.get("hooks") or []:
                if hook.get("type") != "command":
                    continue
                cmd = _resolve(hook.get("command"), cwd)
                if cmd is None:
                    results.append({"event": event, "hook": hook.get("command"),
                                    "ok": False, "error": "command not found"})
                    continue
                try:
                    p = subprocess.run(
                        cmd + [event],
                        input=json.dumps(payload or {}),
                        capture_output=True, text=True, timeout=HOOK_TIMEOUT,
                        cwd=cwd,
                        env=dict(os.environ, FLINT_HOOK_EVENT=event,
                                 **(env or {})),
                    )
                except subprocess.TimeoutExpired:
                    results.append({"event": event, "hook": hook.get("command"),
                                    "ok": False, "error": f"timed out after {HOOK_TIMEOUT}s"})
                    continue
                except OSError as e:
                    results.append({"event": event, "hook": hook.get("command"),
                                    "ok": False, "error": str(e)})
                    continue
                out = (p.stdout or "")[:1000]
                results.append({"event": event, "hook": hook.get("command"),
                                "ok": p.returncode == 0, "exit": p.returncode,
                                "output": out})
    return results


def _hooks_config(env=None):
    env = os.environ if env is None else env
    # agent.config.json beside the checkout, plus FLINT_HOOKS override for tests.
    raw = env.get("FLINT_HOOKS")
    if raw:
        try:
            cfg = json.loads(raw)
            return cfg if isinstance(cfg, dict) else {}
        except json.JSONDecodeError:
            return {}
    candidates = [
        Path(env.get("FLINT_HOOKS_FILE", "")).expanduser(),
        Path(os.getcwd()) / "agent.config.json",
        Path(env.get("FLINT_HOME", "~/.flint")).expanduser() / "agent.config.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                hooks = data.get("hooks")
                return hooks if isinstance(hooks, dict) else {}
            except (OSError, json.JSONDecodeError):
                continue
    return {}