#!/usr/bin/env python3
"""A dollar allowance for editor-initiated model calls.

The swarm itself runs on free models only, by design: `pool()` drops anything that is not
`:free` unless allow_paid is set, so nothing it does can reach the credits on the account. This
wallet is the one deliberate exception. It is a fixed pot of real credits the editor extension
may spend so that a question still gets answered when every free model is rate-limited, gated or
timing out — the case where the panel used to sit there and then give up.

It is cumulative, not daily: $5 stays $5 until it is spent or reset. It is checked before every
request and updated after it, under a file lock, so several models answering the same question in
parallel cannot each spend the last cent. What is recorded is what OpenRouter says the request
cost (`usage.cost`), never an estimate from a price table that can go stale.

    wallet = Wallet(cap=5.0, label="cursor ask")
    ok, why = wallet.check()          # before a request
    wallet.record(0.0031, model)      # after it, with the provider's own number

The counter lives in FLINT_HOME (~/.flint/wallet.json) beside flint's request counter, so it is
shared by every checkout and every editor window on the machine.
"""
import datetime as dt
import json
import os
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:                    # pragma: no cover - Windows
    fcntl = None

STATE = Path(os.environ.get("FLINT_HOME", "~/.flint")).expanduser()
DEFAULT_CAP = 5.0
MIN_CALL = 0.02        # headroom a request must have: a turn that dies halfway through has still
                       # spent its money, so it is better not to start one on the last cent
KEEP = 40              # charges kept for the panel


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Wallet:
    def __init__(self, cap=DEFAULT_CAP, path=None, label=""):
        self.cap = round(max(0.0, float(cap)), 4)
        self.path = Path(path).expanduser() if path else STATE / "wallet.json"
        self.label = label

    # ---------------------------------------------------------------- facts

    def state(self):
        """The ledger. Written with tmp+replace, so a plain read never sees half a file."""
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def spent(self):
        try:
            return round(max(0.0, float(self.state().get("spent", 0.0))), 6)
        except (TypeError, ValueError):
            return 0.0

    def remaining(self):
        return round(max(0.0, self.cap - self.spent()), 6)

    def check(self, need=MIN_CALL):
        """(allowed, reason) for one more request. `need` is the headroom it should have."""
        if self.cap <= 0:
            return False, "the editor has no paid allowance (cap is $0)"
        left = self.remaining()
        if left < min(need, self.cap):
            return False, (f"paid allowance spent: ${self.spent():.4f} of ${self.cap:.2f}. "
                           "Raise it with `bridge.py wallet --cap N`, or reset it with --reset.")
        return True, f"${left:.4f} of ${self.cap:.2f} left"

    # ---------------------------------------------------------------- writes

    @contextmanager
    def _txn(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            d = self.state()
            d.setdefault("spent", 0.0)
            d.setdefault("calls", 0)
            d.setdefault("recent", [])
            yield d
            d["cap"], d["updated"] = self.cap, _now()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)

    def record(self, usd, model="", label=""):
        """Add one real charge and return the snapshot after it. Free models report $0, which is
        recorded as a call but costs nothing."""
        try:
            usd = round(max(0.0, float(usd or 0.0)), 6)
        except (TypeError, ValueError):
            return self.snapshot()
        with self._txn() as d:
            d["calls"] = int(d.get("calls", 0)) + 1
            if usd:
                d["spent"] = round(float(d.get("spent", 0.0)) + usd, 6)
                d["recent"] = (d.get("recent", []) +
                               [{"iso": _now(), "model": model, "usd": usd,
                                 "label": label or self.label}])[-KEEP:]
        return self.snapshot()

    def reset(self):
        """Start the pot over. The cap itself lives in swarm/config.json, not here."""
        with self._txn() as d:
            d.update(spent=0.0, calls=0, recent=[])
        return self.snapshot()

    def snapshot(self):
        d = self.state()
        spent = self.spent()
        return {"cap": self.cap, "spent": spent, "remaining": round(max(0.0, self.cap - spent), 6),
                "calls": int(d.get("calls", 0) or 0), "updated": d.get("updated"),
                "path": str(self.path), "recent": (d.get("recent") or [])[-8:]}

    # ---------------------------------------------------------------- handoff

    def env(self, label=""):
        """What to put in FLINT_WALLET so a flint child spends from this wallet."""
        return json.dumps({"cap": self.cap, "path": str(self.path), "label": label or self.label})

    @classmethod
    def from_env(cls, raw=None):
        """The wallet a parent process handed down, or None when there is none."""
        raw = raw if raw is not None else os.environ.get("FLINT_WALLET")
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return cls(cap=d.get("cap", DEFAULT_CAP), path=d.get("path"), label=d.get("label", ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


if __name__ == "__main__":
    w = Wallet()
    print(json.dumps(w.snapshot(), indent=2))
    print("\nallowed={} — {}".format(*w.check()))
