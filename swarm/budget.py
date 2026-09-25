#!/usr/bin/env python3
"""Daily reserve and optional owner window; flint enforces RPM under a file lock.

Counters are local to FLINT_HOME and reset on the UTC date boundary. They do
not include other clients or machines using the same OpenRouter account.
"""
import datetime as dt
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path(os.environ.get("FLINT_HOME", "~/.flint")).expanduser()
REQUESTS_JSON = STATE / "requests.json"
LOCAL_TZ = ZoneInfo(os.environ.get("FLINT_TZ", "America/New_York"))
UTC = dt.timezone.utc


def _parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


class Budget:
    def __init__(self, cap=None, reserve=10, owner_window=("20:00", "00:00")):
        self.cap = int(cap if cap is not None else os.environ.get("FLINT_DAILY_CAP", "50"))
        self.reserve = reserve
        if not 0 <= reserve < self.cap:
            raise ValueError("reserve must be nonnegative and smaller than daily_cap")
        self.win_start = _parse_hhmm(owner_window[0])
        self.win_end = _parse_hhmm(owner_window[1])

    # ---------------------------------------------------------------- facts

    @staticmethod
    def spent_today():
        """Read the counter flint maintains. Same file, same UTC-day semantics."""
        try:
            d = json.loads(REQUESTS_JSON.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        today = dt.datetime.now(UTC).date().isoformat()
        return d.get("count", 0) if d.get("day") == today else 0

    @staticmethod
    def blocked_until():
        try:
            return json.loads(REQUESTS_JSON.read_text()).get("blocked_until", 0)
        except (OSError, json.JSONDecodeError):
            return 0

    # ---------------------------------------------------------------- window

    def _in_owner_window(self, local):
        s = local.hour * 3600 + local.minute * 60
        if self.win_start <= self.win_end:
            return self.win_start <= s < self.win_end
        return s >= self.win_start or s < self.win_end        # wraps midnight

    def _owner_window_ends(self, local):
        end = local.replace(hour=self.win_end // 3600, minute=(self.win_end % 3600) // 60,
                            second=0, microsecond=0)
        if end <= local:
            end += dt.timedelta(days=1)
        return end

    # ---------------------------------------------------------------- policy

    def check(self, need=1, state=None):
        """(allowed, seconds_to_wait, reason)"""
        now = dt.datetime.now(UTC)
        local = now.astimezone(LOCAL_TZ)

        hard = state.get("blocked_until", 0) if state is not None else self.blocked_until()
        if hard > time.time():
            return False, min(hard - time.time(), 900), "provider daily cap hit — backing off"

        if self._in_owner_window(local):
            ends = self._owner_window_ends(local)
            return False, min((ends - local).total_seconds(), 900), \
                f"owner window until {ends:%H:%M} local"

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + dt.timedelta(days=1)
        usable = max(0, self.cap - self.reserve)
        if state is None:
            spent = self.spent_today()
        else:
            spent = state.get("count", 0) if state.get("day") == now.date().isoformat() else 0

        if spent + need > usable:
            return False, min((day_end - now).total_seconds(), 900), \
                f"day's allowance spent ({spent}/{usable}, {self.reserve} held in reserve)"

        return True, 0, f"within budget ({spent}/{usable}, {self.reserve} reserved)"

    def paid_would_help(self, state=None):
        """True when free capacity is the only thing in the way.

        A paid model can run in that case, so a swarm with a dollar budget switches to one
        instead of idling until midnight. During the owner's window it returns False: that
        pause is a request to leave the machine alone, not a shortage of free requests."""
        now = dt.datetime.now(UTC)
        if self._in_owner_window(now.astimezone(LOCAL_TZ)):
            return False
        allowed, _, _ = self.check(state=state)
        return not allowed

    def snapshot(self):
        now = dt.datetime.now(UTC)
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        spent = self.spent_today()
        usable = max(0, self.cap - self.reserve)
        return {
            "spent_today": spent,
            "usable": usable,
            "cap": self.cap,
            "reserve": self.reserve,
            "remaining": max(0, usable - spent),
            "resets_utc": day_end.isoformat(timespec="minutes"),
            "resets_local": day_end.astimezone(LOCAL_TZ).strftime("%H:%M %Z"),
            "hours_to_reset": round((day_end - now).total_seconds() / 3600, 1),
            "owner_window_now": self._in_owner_window(now.astimezone(LOCAL_TZ)),
        }


if __name__ == "__main__":
    b = Budget()
    print(json.dumps(b.snapshot(), indent=2))
    ok, wait, why = b.check()
    print(f"\nallowed={ok} wait={wait:.0f}s — {why}")
