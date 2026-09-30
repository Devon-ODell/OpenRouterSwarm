#!/usr/bin/env python3
"""Per-request cost receipts, albatross-style (`~/.albatross/routes.jsonl`).

flint.py already banks every charged request into FLINT_CHARGE_FILE (a JSON line per
charge, with the model and OpenRouter's own `cost` figure). That sidecar is transient:
the daemon consumes it after a turn and never keeps the rows. This module is the lasting
ledger — one JSON line per request, append-only, in the change request's Cursor-shaped
location (default `~/.albatross/routes.jsonl`, overridable with FLINT_ROUTES_FILE) —
plus small queries the panel and the reports can use.

Two row shapes are read interchangeably:

- **flint rows** (written by this module via flint.py): `{at, model, backend, usd, local}`
- **albatross rows** (from the real albatross harness): `{kind:"modelCall", timestamp,
  requested_model, actual_model, ...}` — cost appears as `$amount` / `cost_usd` /
  `response_cost_usd` fields when recorded.

Nothing here makes a network call or touches the user's checkout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def routes_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get("FLINT_ROUTES_FILE", "~/.albatross/routes.jsonl")).expanduser()


def record(row, env=None):
    """Append one receipt. `row` is a dict; the `at` timestamp defaults to now.

    The write is a plain append: a torn line never corrupts the ledger, it just
    becomes a line the readers skip. 0600 because a row can carry a model id a
    project may consider private — never a key, those stay out of receipts.
    """
    env = os.environ if env is None else env
    path = routes_path(env)
    row = dict(row)
    row.setdefault("at", time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass  # a receipt is banked, never fatal
    return path


def _row_cost(row):
    """USD cost of one row, whichever shape it has; 0 when it cost nothing."""
    for key in ("usd", "cost", "cost_usd", "response_cost_usd", "$amount"):
        v = (row or {}).get(key)
        if v is None:
            continue
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    # Albatross rows put the billed amount in a `$`-prefixed field name.
    for key, v in (row or {}).items():
        if key.startswith("$") and isinstance(v, (int, float, str)):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return 0.0


def _row_model(row):
    return str((row or {}).get("model") or (row or {}).get("requested_model")
               or (row or {}).get("actual_model") or "?")


def _row_at(row):
    ts = (row or {}).get("at") or (row or {}).get("timestamp")
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    # ISO 8601 ("2026-09-28T18:52:34.153211+00:00")
    import datetime as _dt
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = _dt.datetime.strptime(str(ts).strip(), fmt)
            return d.timestamp()
        except ValueError:
            continue
    return None


def _all(env=None):
    path = routes_path(env)
    if not path.is_file():
        return []
    out = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    except OSError:
        return []
    return out


def tail(limit=100, env=None):
    """The newest receipts, newest first (what the panel's cost footer wants)."""
    rows = _all(env=env)
    rows.sort(key=lambda r: _row_at(r) or 0, reverse=True)
    return rows[: int(limit)]


def summarize(since=None, env=None):
    """Totals for the panel and the report: per-model spend and a running total.

    `since` is an epoch or None (the whole ledger). Local rows (cost 0) are counted
    separately so the OpenRouter spend number is never diluted by free requests.
    """
    rows = [r for r in _all(env=env) if since is None or ((_row_at(r) or 0) >= since)]
    total = 0.0
    by_model = {}
    local_requests = 0
    paid_requests = 0
    for r in rows:
        usd = _row_cost(r)
        total += usd
        model = _row_model(r)
        m = by_model.setdefault(model, {"requests": 0, "usd": 0.0})
        m["requests"] += 1
        m["usd"] = round(m["usd"] + usd, 6)
        if r.get("local") or str(r.get("requested_backend") or "").startswith(("ollama", "lm", "mlx", "llama")):
            local_requests += 1
        elif usd:
            paid_requests += 1
    return {
        "total_usd": round(total, 6),
        "requests": len(rows),
        "local_requests": local_requests,
        "paid_requests": paid_requests,
        "by_model": sorted(({"model": k, **v} for k, v in by_model.items()),
                           key=lambda x: -x["usd"])[:20],
    }