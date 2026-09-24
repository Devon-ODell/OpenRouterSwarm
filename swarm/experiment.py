#!/usr/bin/env python3
"""Does the MIT OpenCourseWare corpus make the swarm's models better? A randomized comparison.

Before its implementer runs, every attempt is assigned an arm (swarmd.mit_arm): "on" gets
lecture-card excerpts in its prompt plus the `study` tool, "off" gets neither. Queue, models,
reviewers and test gates are shared, so a difference between arms estimates the corpus's
effect. The unit is the attempt; one task can be attempted under both arms. Planner prompts
are not randomized, so a task idea may still come from the corpus ("scholar" persona).

    python3 experiment.py swarm/state/<repo>-<hash>/journal.jsonl
"""
import json
import math
import sys
from collections import Counter
from pathlib import Path

LANDED = "accepted"
FAULTY = ("weakened_tests", "tests_failed", "rejected")
MIN_PER_ARM = 30          # below this, say plainly that the numbers are anecdotes


def attempts(journal, since=0.0):
    """Attempt rows from a swarm journal that belong to the experiment."""
    out = []
    try:
        lines = Path(journal).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "attempt" and r.get("mit") in ("on", "off") and r.get("t", 0) >= since:
            out.append(r)
    return out


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    p, d = k / n, 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher(a, b, c, d):
    """Two-sided Fisher exact p-value for the table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    total = n1 + n2
    if n1 == 0 or n2 == 0 or k in (0, total):
        return 1.0

    def p(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(total, k)
    observed = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9)))


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(sum(xs) / len(xs), 3) if xs else None


def arm(rows):
    n = len(rows)
    landed = sum(r.get("stage") == LANDED for r in rows)
    judged = [r.get("scores") or {} for r in rows if r.get("stage") == LANDED and r.get("scores")]
    lo, hi = wilson(landed, n)
    return {"attempts": n, "landed": landed,
            "landed_rate": round(landed / n, 3) if n else None,
            "landed_ci95": [round(lo, 3), round(hi, 3)] if n else None,
            "faulty": sum(r.get("stage") in FAULTY for r in rows),
            "mean_reward": _mean(r.get("reward") for r in rows),
            "impact": _mean(s.get("impact") for s in judged),
            "creativity": _mean(s.get("creativity") for s in judged),
            "quality": _mean(s.get("quality") for s in judged),
            "study_calls_per_attempt": _mean(r.get("study_calls", 0) for r in rows),
            "excerpts_injected": sum(bool(r.get("injected")) for r in rows),
            "stages": dict(Counter(r.get("stage", "?") for r in rows))}


def summary(journal, since=0.0):
    rows = attempts(journal, since)
    on = [r for r in rows if r["mit"] == "on"]
    off = [r for r in rows if r["mit"] == "off"]
    a_on, a_off = arm(on), arm(off)
    out = {"on": a_on, "off": a_off, "difference": None, "p_value": None, "by_model": {}}
    if on and off:
        p1, p2 = a_on["landed"] / len(on), a_off["landed"] / len(off)
        se = math.sqrt(p1 * (1 - p1) / len(on) + p2 * (1 - p2) / len(off))
        out["difference"] = {"landed_rate": round(p1 - p2, 3),
                             "ci95": [round(p1 - p2 - 1.96 * se, 3), round(p1 - p2 + 1.96 * se, 3)]}
        if a_on["mean_reward"] is not None and a_off["mean_reward"] is not None:
            out["difference"]["mean_reward"] = round(a_on["mean_reward"] - a_off["mean_reward"], 3)
        out["p_value"] = round(fisher(a_on["landed"], len(on) - a_on["landed"],
                                      a_off["landed"], len(off) - a_off["landed"]), 4)
    for model in sorted({r.get("implementer") for r in rows if r.get("implementer")}):
        mine = [r for r in rows if r.get("implementer") == model]
        out["by_model"][model] = {side: arm([r for r in mine if r["mit"] == side]) for side in ("on", "off")}
    used = [r for r in on if r.get("study_calls")]
    unused = [r for r in on if not r.get("study_calls")]
    out["on_by_study_use"] = {"used_study": arm(used), "did_not": arm(unused)}
    out["verdict"] = verdict(out)
    return out


def verdict(s):
    n_on, n_off = s["on"]["attempts"], s["off"]["attempts"]
    if not n_on or not n_off:
        return f"No comparison yet: {n_on} attempt(s) with the corpus, {n_off} without."
    d = s["difference"]
    line = (f"Landed with the corpus {s['on']['landed']}/{n_on}, without {s['off']['landed']}/{n_off}: "
            f"difference {d['landed_rate']:+.0%} (95% CI {d['ci95'][0]:+.0%} to {d['ci95'][1]:+.0%}), "
            f"Fisher p = {s['p_value']:.3g}.")
    if min(n_on, n_off) < MIN_PER_ARM:
        return line + f" Too few attempts to conclude anything; aim for {MIN_PER_ARM}+ per arm."
    if d["ci95"][0] > 0:
        return line + " The corpus arm lands work more often."
    if d["ci95"][1] < 0:
        return line + " The corpus arm lands work less often."
    return line + " No detectable difference in landing rate so far."


def _cell(a):
    if not a["attempts"]:
        return "–"
    return f"{a['landed']}/{a['attempts']}" + (f", r̄ {a['mean_reward']:.2f}" if a["mean_reward"] is not None else "")


def markdown(s):
    rows = [("attempts", "attempts"), ("landed", "landed"), ("landed rate (95% CI)", None),
            ("faulty code (hung)", "faulty"), ("mean reward", "mean_reward"), ("judge impact", "impact"),
            ("judge creativity", "creativity"), ("judge quality", "quality"),
            ("study calls / attempt", "study_calls_per_attempt"), ("prompts with excerpts", "excerpts_injected")]
    out = [s["verdict"], "", "| | with MIT corpus | without |", "|---|---:|---:|"]
    for label, key in rows:
        if key is None:
            cells = [f"{a['landed_rate']:.0%} ({a['landed_ci95'][0]:.0%}–{a['landed_ci95'][1]:.0%})"
                     if a["attempts"] else "–" for a in (s["on"], s["off"])]
        else:
            cells = ["–" if s[side][key] is None else str(s[side][key]) for side in ("on", "off")]
        out.append(f"| {label} | {cells[0]} | {cells[1]} |")
    if s["by_model"]:
        out += ["", "Per implementer model (landed/attempts, mean reward):", "",
                "| model | with corpus | without |", "|---|---:|---:|"]
        for model, arms in s["by_model"].items():
            out.append(f"| `{model}` | {_cell(arms['on'])} | {_cell(arms['off'])} |")
    u, n = s["on_by_study_use"]["used_study"], s["on_by_study_use"]["did_not"]
    if u["attempts"] or n["attempts"]:
        out += ["", f"Within the corpus arm, attempts that called `study` landed {u['landed']}/{u['attempts']}, "
                f"those that did not {n['landed']}/{n['attempts']}. Models choose whether to study, so this split "
                "is observational, not randomized."]
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(markdown(summary(sys.argv[1])))
