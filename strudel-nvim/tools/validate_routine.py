#!/usr/bin/env python3
"""Dependency-free validator for a filming cue score JSON.

Checks the canonical two-hour routine for structural and semantic correctness:
correct total duration, at least 24 unique chronological cues, every required
field present and typed correctly, monotonic and in-range scheduled minutes,
valid modes and energy values, unique cue ids, and that the required arc
sections are all represented.

Usage:
    python3 validate_routine.py <path-to-routine.json>

Exits 0 when the routine is valid, 1 otherwise.
"""
import json
import sys
import os

VALID_MODES = {"generate", "drift", "chill", "fix"}
REQUIRED_ARC = {"opening", "rise", "midpoint_reset", "peak", "outro"}


def error(msg):
    print(f"validation error: {msg}", file=sys.stderr)


def validate(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        error(f"cannot load JSON from {path}: {exc}")
        return 1

    if not isinstance(data, dict):
        error("top-level value must be a JSON object")
        return 1

    duration = data.get("duration_minutes")
    if not isinstance(duration, int) or duration != 120:
        error(f"duration_minutes must be exactly 120, got {duration!r}")
        return 1

    cues = data.get("cues")
    if not isinstance(cues, list):
        error("cues must be a list")
        return 1

    if len(cues) < 24:
        error(f"need at least 24 cues, found {len(cues)}")
        return 1

    seen_ids = set()
    seen_minutes = []
    modes_seen = set()

    for i, cue in enumerate(cues):
        if not isinstance(cue, dict):
            error(f"cue at index {i} is not an object")
            return 1

        cid = cue.get("id")
        if cid is None or not isinstance(cid, str) or not cid.strip():
            error(f"cue at index {i} has a missing or invalid id")
            return 1
        if cid in seen_ids:
            error(f"duplicate cue id: {cid}")
            return 1
        seen_ids.add(cid)

        minute = cue.get("minute")
        if not isinstance(minute, int) or minute < 0 or minute > 119:
            error(f"cue '{cid}' has an out-of-range minute: {minute!r} (must be 0-119)")
            return 1
        seen_minutes.append(minute)

        mode = cue.get("mode")
        if mode not in VALID_MODES:
            error(f"cue '{cid}' has an invalid mode: {mode!r}")
            return 1
        modes_seen.add(mode)

        instruction = cue.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            error(f"cue '{cid}' has a missing or empty instruction")
            return 1

        energy = cue.get("energy")
        if not isinstance(energy, (int, float)) or energy < 0 or energy > 1:
            error(f"cue '{cid}' has an invalid energy: {energy!r} (must be 0-1)")
            return 1

        camera = cue.get("camera")
        if not isinstance(camera, str) or not camera.strip():
            error(f"cue '{cid}' has a missing or empty camera note")
            return 1

    # Monotonic check: cues must appear in chronological order.
    for i in range(1, len(seen_minutes)):
        if seen_minutes[i] <= seen_minutes[i - 1]:
            prev = cues[i - 1]["id"]
            cur = cues[i]["id"]
            error(f"non-monotonic ordering: {prev} at minute {seen_minutes[i-1]} comes before {cur} at minute {seen_minutes[i]}")
            return 1

    # Outro must appear after minute 115.
    outro_minutes = [c["minute"] for c in cues if c["id"].startswith("outro-")]
    if not outro_minutes:
        error("no outro cue found")
        return 1
    if min(outro_minutes) <= 115:
        error(f"outro cues must come after minute 115, found minute {min(outro_minutes)}")
        return 1

    # Check that all required arc sections appear somewhere in the ids.
    arc_found = set()
    for cue in cues:
        for arc in REQUIRED_ARC:
            if cue["id"].startswith(arc + "-"):
                arc_found.add(arc)
    missing = REQUIRED_ARC - arc_found
    if missing:
        error(f"missing required arc sections: {', '.join(sorted(missing))}")
        return 1

    modes_missing = REQUIRED_ARC & set()  # no-op, kept for clarity
    if not modes_seen >= VALID_MODES:
        pass  # not required that all modes appear

    print(f"valid: {duration} minutes, {len(cues)} cues")
    return 0


def main():
    if len(sys.argv) != 2:
        print(f"usage: {os.path.basename(sys.argv[0])} <routine.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(validate(sys.argv[1]))


if __name__ == "__main__":
    main()
