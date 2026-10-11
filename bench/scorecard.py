#!/usr/bin/env python3
"""Score a benchmark's fixed tasks and append one row to its history.

    .venv/bin/python bench/scorecard.py skyring            # print and append
    .venv/bin/python bench/scorecard.py skyring --dry-run  # print only

Reads (never writes) the repository's swarm state: done.jsonl, queue.jsonl and journal.jsonl,
for the task ids bench/enqueue.py recorded and every task split from them. Appends a row to
bench/history/<benchmark>.jsonl stamped with the harness commit and the target's swarm/trunk,
so the same packets can be compared across harness versions, model pools and configs.

Per task: final status, attempts, request starts, dollars, minutes from queueing to landing,
and the implementer that landed it. Overall: landed share, attempts and requests per landed
task, failure classes, and landings per model.
"""
import argparse
import collections
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swarm import swarmd  # noqa: E402
sys.path.insert(0, str(ROOT / "swarm"))
from app import SwarmApplication  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def score(benchmark):
    book = json.loads((ROOT / "bench" / "history" / f"{benchmark}-ids.json").read_text())
    waves = sorted({v["wave"] for v in book["tasks"].values()})
    repo = json.loads((ROOT / "bench" / benchmark / f"wave{waves[0]}.json").read_text())["repo"]
    app = SwarmApplication.for_repo(repo, daemon=swarmd)
    done = {r["id"]: r for r in app.done()}
    pending = {r["id"]: r for r in app.queue().pending()}
    journal = app.journal()
    attempts = collections.defaultdict(list)
    for j in journal:
        if j.get("event") == "attempt":
            attempts[j["id"]].append(j)

    def family(tid):
        """The task and everything split from it."""
        rows = list(done.values()) + list(pending.values())
        return [tid] + [r["id"] for r in rows if r.get("root") == tid and r["id"] != tid]

    tasks, classes, models = [], collections.Counter(), collections.Counter()
    for ref, meta in book["tasks"].items():
        ids = family(meta["id"])
        rows = [done.get(i) or pending.get(i) for i in ids]
        tries = [a for i in ids for a in attempts[i]]
        root = done.get(meta["id"]) or pending.get(meta["id"]) or {}
        status = root.get("status") or ("pending" if meta["id"] in pending else "unknown")
        children = [r for r in rows[1:] if r]
        if status == "split" and children:
            status = "landed via split" if all(r.get("status") == "done" for r in children) else \
                f"split ({sum(r.get('status') == 'done' for r in children)}/{len(children)} landed)"
        landed = status in ("done", "landed via split")
        for a in tries:
            if a.get("stage") != "accepted":
                classes[a.get("failure_class") or "task"] += 1
        winner = next((a.get("implementer") for a in reversed(tries) if a.get("stage") == "accepted"), None)
        if winner:
            models[winner] += 1
        finished = max((r.get("finished", 0) for r in rows if r and r.get("status") == "done"), default=0)
        tasks.append({
            "ref": ref, "id": meta["id"], "wave": meta["wave"], "status": "landed" if status == "done" else status,
            "attempts": len(tries), "requests": sum(int(a.get("requests") or 0) for a in tries),
            "usd": round(sum(float(a.get("usd") or 0) for a in tries), 4),
            "minutes_to_land": round((finished - meta["queued_at"]) / 60, 1) if landed and finished else None,
            "implementer": winner, "split_into": len(children)})
    landed = [t for t in tasks if t["status"] in ("landed", "landed via split")]
    settled = [t for t in tasks if t["status"] not in ("pending",)]
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "benchmark": benchmark, "waves": waves,
        "harness": git(ROOT, "rev-parse", "--short", "HEAD"),
        "target_trunk": git(repo, "rev-parse", "--short", "swarm/trunk") or None,
        "tasks": len(tasks), "landed": len(landed), "pending": len(tasks) - len(settled),
        "landed_share": round(len(landed) / len(tasks), 3) if tasks else 0,
        "attempts": sum(t["attempts"] for t in tasks),
        "attempts_per_landed": round(sum(t["attempts"] for t in tasks) / len(landed), 2) if landed else None,
        "requests": sum(t["requests"] for t in tasks),
        "requests_per_landed": round(sum(t["requests"] for t in tasks) / len(landed), 1) if landed else None,
        "usd": round(sum(t["usd"] for t in tasks), 4),
        "median_minutes_to_land": statistics.median([t["minutes_to_land"] for t in landed if t["minutes_to_land"]])
        if any(t["minutes_to_land"] for t in landed) else None,
        "failure_classes": dict(classes), "landings_by_model": dict(models), "per_task": tasks}
    return row


def show(row):
    print(f"{row['benchmark']} @ harness {row['harness']}, target trunk {row['target_trunk']}  ({row['at']})")
    print(f"landed {row['landed']}/{row['tasks']} ({row['landed_share']:.0%}), {row['pending']} pending; "
          f"{row['attempts']} attempts, {row['requests']} requests, ${row['usd']:.2f}")
    print(f"per landed task: {row['attempts_per_landed']} attempts, {row['requests_per_landed']} requests; "
          f"median {row['median_minutes_to_land']} min to land")
    print(f"failure classes: {row['failure_classes'] or '-'}   landings by model: {row['landings_by_model'] or '-'}")
    for t in row["per_task"]:
        print(f"  {t['ref']:24} {t['status']:22} attempts {t['attempts']:2}  requests {t['requests']:4}"
              f"  {t['implementer'] or ''}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("benchmark")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    row = score(a.benchmark)
    show(row)
    if not a.dry_run:
        with open(ROOT / "bench" / "history" / f"{a.benchmark}.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
