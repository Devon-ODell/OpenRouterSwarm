#!/usr/bin/env python3
"""A/B test for N6: what an accepted change with no usable judge scores should teach the bandit.

    .venv/bin/python bench/unscored_ab.py                    # every scenario, 2000 paired replicates
    .venv/bin/python bench/unscored_ab.py --reps 500 --horizon 300 --no-record

The arms are learn.UNSCORED_POLICIES, scored through learn.unscored_update, which is the code
the daemon runs (config `unscored_reward`):

    neutral  0.6 + novelty at full weight (what the swarm did until 2026-10-11)
    floor    the 0.4 floor + novelty at weight 1
    impute   the implementer's own mean judged reward so far, at full weight (the default
             since 2026-10-11, on rule v2's verdict)
    skip     no update

It takes two measurements, both from the judged landings in every repository's journal:

1. Calibration. Suppose a landing's judge had failed: what would each policy have credited,
   against what the judge actually gave? For impute, the mean leaves the landing itself out.
2. Bandit replay. Thompson sampling with the ledger's own update rule (PRIOR, DISCOUNT, the
   reward and penalty weights) over the implementers with enough history. Their accept rates
   and judged rewards are estimated from that history, and the judge fails at the rates the
   scenario sets. Each replicate draws every model's k-th outcome up front (common random
   numbers), so the policies are compared on identical luck. The metric is regret per attempt:
   the expected reward of the best implementer minus that of the one drawn. An implementer's
   expected reward is its accept rate times its mean judged reward.

Decision rule, fixed before the first run:
- The primary scenario is "per-model": judge failure rates estimated per implementer from
  history, because the history shows failures clustering on some implementers.
- A policy replaces neutral only if both hold:
  - In the primary scenario, its paired regret difference against neutral has a 95% bootstrap
    interval entirely below zero.
  - In no other scenario is it worse than neutral by more than 5% of neutral's regret with an
    interval entirely above that margin.
- Among the policies that qualify, the lowest primary regret wins. If none qualifies, neutral
  stays.

Amendment (2026-10-11, after the first recorded run). That run's v1 rule picked `skip`, which
won the primary scenario. But in this history the implementers whose work is most often
unjudged (laguna q=0.52, dots q=0.31) are weaker ones. Skipping their unjudged landings made
them look worse, and that happened to help. A post-hoc check with the same failure rates on the
strongest implementers instead showed skip 18% worse than neutral and impute 20% better. That
check is now the "per-model-reversed" scenario. Rule v2 replaces neutral with a policy only if
both hold:
- In no scenario is it worse than neutral by more than the margin, with an interval entirely
  above it.
- In most scenarios it is better, with an interval entirely below zero.

Among the policies that qualify, the lowest mean regret across scenarios wins. Every run reports
both rules, so v1's verdict is never hidden. v2 was written after seeing data; weigh it as such.

Each run appends one row to bench/history/unscored_ab.jsonl, and the data grows with every
shift. Live runs can also split tasks between two policies (`unscored_reward: "ab"`,
`unscored_ab_arms`). Every unscored reward row then records the policy it used, so the
calibration step can later be checked against live outcomes.
"""
import argparse
import collections
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - bench tooling, not the daemon
    sys.exit("bench/unscored_ab.py needs numpy (.venv/bin/pip install numpy)")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "swarm"))
import learn  # noqa: E402

PRIMARY = "per-model"
MARGIN = 0.05            # "not worse" means within 5% of neutral's regret
SHRINK = 4               # pseudo-observations pulling sparse per-model estimates to the pool


# ------------------------------------------------------------------ history

def load_history(state_root):
    """Per-implementer outcomes from every repository's journal."""
    models = collections.defaultdict(lambda: {"acc": 0, "fail": 0, "judged": [], "unscored": 0})
    for path in sorted(Path(state_root).glob("*/journal.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = r.get("implementer")
            if not m:
                continue
            if r.get("event") == "attempt":
                if r.get("stage") == "accepted":
                    models[m]["acc"] += 1
                elif r.get("failure_class") in ("task", "model"):
                    models[m]["fail"] += 1       # harness losses teach the bandit nothing
            elif r.get("event") == "reward" and r.get("stage") == "accepted":
                if all(k in (r.get("scores") or {}) for k in learn.WEIGHTS):
                    models[m]["judged"].append((float(r["reward"]), float(r.get("novelty") or 0)))
                else:
                    models[m]["unscored"] += 1
    return dict(models)


def world(history, min_attempts=3):
    """The simulated implementers, estimated from history and shrunk toward the pool."""
    judged = [x for h in history.values() for x in h["judged"]]
    if len(judged) < 5:
        sys.exit(f"only {len(judged)} judged landings in history; too few to estimate anything")
    rewards = [r for r, _ in judged]
    pooled, spread = statistics.fmean(rewards), statistics.pstdev(rewards)
    landed = sum(h["acc"] for h in history.values())
    q_all = sum(h["unscored"] for h in history.values()) / max(1, sum(
        h["unscored"] + len(h["judged"]) for h in history.values()))
    names, p, mu, q_model = [], [], [], []
    for m, h in sorted(history.items()):
        n = h["acc"] + h["fail"]
        if n < min_attempts:
            continue
        names.append(m)
        p.append((h["acc"] + 1) / (n + 2))
        rs = [r for r, _ in h["judged"]]
        mu.append((sum(rs) + SHRINK * pooled) / (len(rs) + SHRINK))
        seen = h["unscored"] + len(h["judged"])
        q_model.append((h["unscored"] + SHRINK * q_all) / (seen + SHRINK))
    q_model = np.array(q_model)
    # The same rates, the highest given to the strongest implementer (p × mu).
    q_reversed = np.empty_like(q_model)
    q_reversed[np.argsort(np.array(p) * np.array(mu))] = np.sort(q_model)
    resid = np.array([r - pooled for r, _ in judged])
    nov = np.array([n for _, n in judged])
    return {"names": names, "p": np.array(p), "mu": np.array(mu), "q_model": q_model,
            "q_reversed": q_reversed,
            "q_all": q_all, "pooled": pooled, "spread": spread, "resid": resid, "nov": nov,
            "judged": len(judged), "landed": landed,
            "unscored": sum(h["unscored"] for h in history.values())}


SCENARIOS = {
    "independent": "every judge fails at the observed overall rate",
    "per-model": "each implementer's work fails to be judged at its own observed rate",
    "fails-on-strong": "failures more likely on high-reward work (rho +0.5)",
    "fails-on-weak": "failures more likely on low-reward work (rho -0.5)",
    "frequent": "every judge fails 40% of the time (SkyRing before salvage: 2 of 5)",
    "per-model-reversed": "the observed per-model rates, the highest on the strongest implementers",
}


def judge_fails(scenario, w, pick, true_r, u):
    q = w["q_all"]
    if scenario == "per-model":
        q = w["q_model"][pick]
    elif scenario == "per-model-reversed":
        q = w["q_reversed"][pick]
    elif scenario == "frequent":
        q = 0.4
    elif scenario in ("fails-on-strong", "fails-on-weak"):
        rho = 0.5 if scenario == "fails-on-strong" else -0.5
        z = (true_r - w["pooled"]) / max(w["spread"], 1e-6)
        q = np.clip(w["q_all"] * (1 + rho * z), 0, 1)
    return u < q


# ------------------------------------------------------------------ calibration

def calibration(history):
    """What each policy would have credited a judged landing had its judge failed."""
    rows = [(m, r, n) for m, h in history.items() for r, n in h["judged"]]
    every = [r for _, r, _ in rows]
    out = {}
    for policy in learn.UNSCORED_POLICIES:
        errors, weights = [], []
        for i, (m, r, n) in enumerate(rows):
            others = [x for j, (mm, x, _) in enumerate(rows) if j != i and mm == m]
            pool = [x for j, x in enumerate(every) if j != i]
            mean = statistics.fmean(others or pool) if (others or pool) else None
            update = learn.unscored_update(policy, n, model_mean=mean)
            if update is None:
                continue
            errors.append(update[0] - r)
            weights.append(update[1])
        out[policy] = ({"bias": round(statistics.fmean(errors), 4),
                        "mae": round(statistics.fmean(abs(e) for e in errors), 4),
                        "weight": weights[0]} if errors else
                       {"bias": None, "mae": None, "weight": 0.0})
    return out


# ------------------------------------------------------------------ replay

def replay(policy, scenario, w, reps, horizon, seed):
    """Regret per attempt and share of attempts on the best implementer, one value per replicate."""
    K = len(w["names"])
    luck = np.random.default_rng(seed)             # identical for every policy
    accept_u = luck.random((reps, K, horizon))
    judge_u = luck.random((reps, K, horizon))
    draw = luck.integers(0, len(w["resid"]), (reps, K, horizon))
    sampler = np.random.default_rng(seed + 1)
    a = np.full((reps, K), learn.PRIOR)
    b = np.full((reps, K), learn.PRIOR)
    pulls = np.zeros((reps, K), dtype=int)
    judged_sum = np.zeros((reps, K))
    judged_n = np.zeros((reps, K))
    value = w["p"] * w["mu"]
    best = int(np.argmax(value))
    regret = np.zeros(reps)
    on_best = np.zeros(reps)
    rows = np.arange(reps)
    penalty = learn.PENALTY_WEIGHT["tests_failed"]
    for _ in range(horizon):
        pick = np.argmax(sampler.beta(a, b), axis=1)
        k = pulls[rows, pick]
        pulls[rows, pick] += 1
        regret += value[best] - value[pick]
        on_best += pick == best
        idx = draw[rows, pick, k]
        true_r = np.clip(w["mu"][pick] + w["resid"][idx], 0.4, 1.0)
        nov = w["nov"][idx]
        landed = accept_u[rows, pick, k] < w["p"][pick]
        unjudged = landed & judge_fails(scenario, w, pick, true_r, judge_u[rows, pick, k])
        r = np.where(landed, true_r, 0.0)
        wt = np.where(landed, learn.REWARD_WEIGHT, penalty)
        learns = np.ones(reps, dtype=bool)
        for i in np.nonzero(unjudged)[0]:
            m = pick[i]
            own = judged_sum[i, m] / judged_n[i, m] if judged_n[i, m] else None
            total = judged_n[i].sum()
            mean = own if own is not None else (judged_sum[i].sum() / total if total else None)
            update = learn.unscored_update(policy, float(nov[i]), model_mean=mean)
            if update is None:
                learns[i] = False
            else:
                r[i], wt[i] = update
        seen = landed & ~unjudged
        judged_sum[rows[seen], pick[seen]] += true_r[seen]
        judged_n[rows[seen], pick[seen]] += 1
        sel = rows[learns]
        arm = pick[learns]
        a[sel, arm] = learn.PRIOR + learn.DISCOUNT * (a[sel, arm] - learn.PRIOR) + wt[learns] * r[learns]
        b[sel, arm] = learn.PRIOR + learn.DISCOUNT * (b[sel, arm] - learn.PRIOR) + wt[learns] * (1 - r[learns])
    return regret / horizon, on_best / horizon


def interval(diff, seed, n=2000):
    """Mean and 95% bootstrap interval of a paired difference."""
    rng = np.random.default_rng(seed)
    means = diff[rng.integers(0, len(diff), (n, len(diff)))].mean(axis=1)
    return float(diff.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


V1_SCENARIOS = ("independent", "per-model", "fails-on-strong", "fails-on-weak", "frequent")


def harms(results, policy, scenarios):
    return [s for s in scenarios
            if results[s][policy]["vs_neutral"][1] > MARGIN * results[s]["neutral"]["regret"]]


def decide_v2(results):
    """Rule v2 (the amendment in the module docstring): never harmful, better in most."""
    qualified = []
    for policy in learn.UNSCORED_POLICIES:
        if policy == "neutral" or harms(results, policy, results):
            continue
        better = [s for s in results if results[s][policy]["vs_neutral"][2] < 0]
        if len(better) > len(results) / 2:
            qualified.append(policy)
    if not qualified:
        return "neutral", "no policy was better in most scenarios without harm in any"
    mean = {p: statistics.fmean(results[s][p]["regret"] for s in results) for p in qualified}
    win = min(qualified, key=mean.get)
    return win, f"qualified: {', '.join(qualified)}; lowest mean regret: {win}"


def decide(results):
    """Rule v1, the one fixed before the first run, over the scenarios it was fixed for."""
    qualified = []
    for policy in learn.UNSCORED_POLICIES:
        if policy == "neutral":
            continue
        _, lo, hi = results[PRIMARY][policy]["vs_neutral"]
        if hi >= 0:
            continue
        harmed = harms(results, policy, [s for s in V1_SCENARIOS if s != PRIMARY])
        if not harmed:
            qualified.append(policy)
    if not qualified:
        return "neutral", "no policy beat neutral in the primary scenario without harm elsewhere"
    win = min(qualified, key=lambda p: results[PRIMARY][p]["regret"])
    return win, f"qualified: {', '.join(qualified)}; lowest primary regret: {win}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--horizon", type=int, default=200, help="attempts per replicate")
    ap.add_argument("--seed", type=int, default=20261011)
    ap.add_argument("--state", default=str(ROOT / "swarm" / "state"))
    ap.add_argument("--no-record", action="store_true", help="do not append to bench/history")
    a = ap.parse_args(argv)

    history = load_history(a.state)
    w = world(history)
    print(f"history: {w['landed']} landings, {w['judged']} judged, {w['unscored']} unscored "
          f"({w['q_all']:.0%}); judged reward {w['pooled']:.3f} ± {w['spread']:.3f}")
    print(f"implementers ({len(w['names'])}): " + ", ".join(
        f"{n.split('/')[-1]} p={p:.2f} mu={m:.3f} q={q:.2f}"
        for n, p, m, q in zip(w["names"], w["p"], w["mu"], w["q_model"])))

    cal = calibration(history)
    print("\ncalibration (credited minus judged, had the judge failed):")
    for policy, row in cal.items():
        print(f"  {policy:8} bias {row['bias'] if row['bias'] is not None else '—':>8}  "
              f"mae {row['mae'] if row['mae'] is not None else '—':>7}  weight {row['weight']}")

    results = {}
    for s_i, scenario in enumerate(SCENARIOS):
        seed = a.seed + 1000 * s_i
        runs = {p: replay(p, scenario, w, a.reps, a.horizon, seed) for p in learn.UNSCORED_POLICIES}
        base = runs["neutral"][0]
        results[scenario] = {}
        for policy, (regret, on_best) in runs.items():
            results[scenario][policy] = {
                "regret": round(float(regret.mean()), 5),
                "on_best": round(float(on_best.mean()), 4),
                "vs_neutral": [round(x, 5) for x in interval(regret - base, seed + 7)]}
    print(f"\nregret per attempt, {a.reps} paired replicates of {a.horizon} attempts "
          f"(difference vs neutral, 95% interval):")
    for scenario, row in results.items():
        print(f"  {scenario}{' (primary)' if scenario == PRIMARY else ''}: {SCENARIOS[scenario]}")
        for policy, r in row.items():
            d, lo, hi = r["vs_neutral"]
            vs = "" if policy == "neutral" else f"  {d:+.5f} [{lo:+.5f}, {hi:+.5f}]"
            print(f"    {policy:8} {r['regret']:.5f}  best-share {r['on_best']:.3f}{vs}")
    choice, why = decide(results)
    choice2, why2 = decide_v2(results)
    print(f"\nrule v1 (pre-registered): {choice} — {why}")
    print(f"rule v2 (amended 2026-10-11): {choice2} — {why2}")
    print(f"decision: {choice2}")

    if not a.no_record:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        row = {"at": time.time(), "harness": commit, "reps": a.reps, "horizon": a.horizon,
               "seed": a.seed, "landed": w["landed"], "judged": w["judged"],
               "unscored": w["unscored"], "implementers": w["names"], "calibration": cal,
               "results": results, "rule_v1": [choice, why], "rule_v2": [choice2, why2],
               "decision": choice2}
        out = ROOT / "bench" / "history" / "unscored_ab.jsonl"
        with open(out, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"recorded in {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
