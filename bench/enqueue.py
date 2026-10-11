#!/usr/bin/env python3
"""Queue one wave of a benchmark's fixed packets on its repository's swarm.

    .venv/bin/python bench/enqueue.py bench/skyring/wave1.json

Tasks go through SwarmApplication.add_task, the same locked queue path `swarm add` uses, with
each packet's acceptance criteria, path scope and dependencies. The task id of every packet is
recorded in bench/history/<benchmark>-ids.json so bench/scorecard.py can score exactly these
tasks (and anything the decomposer split them into) across runs. Re-running is safe: packets
already recorded are skipped.

A wave with `"amends": "<wave>"` repairs packets already queued instead of adding new ones: each
packet's detail, acceptance and allowed_paths replace the queued task's (TaskQueue.edit, which also
lifts any hold), the task keeps its id, and the ids file records which wave it now runs as.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swarm import swarmd  # noqa: E402
sys.path.insert(0, str(ROOT / "swarm"))
from app import SwarmApplication  # noqa: E402


def harness_commit():
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def main(path):
    wave = json.loads(Path(path).read_text())
    ids_path = ROOT / "bench" / "history" / f"{wave['benchmark']}-ids.json"
    ids_path.parent.mkdir(parents=True, exist_ok=True)
    book = json.loads(ids_path.read_text()) if ids_path.exists() else {"tasks": {}}
    app = SwarmApplication.for_repo(wave["repo"], daemon=swarmd)
    added = 0
    for t in wave["tasks"]:
        if wave.get("amends"):
            added += amend(app, book, wave, t)
            ids_path.write_text(json.dumps(book, indent=2) + "\n")
            continue
        if t["ref"] in book["tasks"]:
            print(f"skip   {t['ref']}: already queued as {book['tasks'][t['ref']]['id']}")
            continue
        deps = [book["tasks"][d]["id"] for d in t.get("depends_on", [])]
        row = app.add_task(t["title"], t["detail"], t["kind"], priority=t.get("priority", 1),
                           acceptance=t["acceptance"], allowed_paths=t.get("allowed_paths"),
                           depends_on=deps or None, origin="human")
        if not row:
            print(f"FAILED {t['ref']}: not queued (queue full or duplicate title)")
            continue
        book["tasks"][t["ref"]] = {"id": row["id"], "wave": wave["wave"], "backlog": t.get("backlog"),
                                   "queued_at": time.time(), "harness": harness_commit()}
        added += 1
        print(f"queued {t['ref']}: {row['id']}")
        ids_path.write_text(json.dumps(book, indent=2) + "\n")
    print(f"{added} {'amended' if wave.get('amends') else 'queued'}; "
          f"ids in {ids_path.relative_to(ROOT)}")
    return 0


def amend(app, book, wave, t):
    """Replace a queued packet's detail, acceptance and scope with the amending wave's."""
    meta = book["tasks"].get(t["ref"])
    if meta is None:
        print(f"FAILED {t['ref']}: wave {wave['amends']} never queued it")
        return 0
    if meta.get("amended") == wave["wave"]:
        print(f"skip   {t['ref']}: already amended to wave {wave['wave']}")
        return 0
    try:
        app.queue().edit(meta["id"], detail=t["detail"], acceptance=t["acceptance"],
                         allowed_paths=t.get("allowed_paths"))
    except (KeyError, ValueError) as e:
        print(f"FAILED {t['ref']}: {meta['id']} not amended ({e})")
        return 0
    meta.update(amended=wave["wave"], amended_at=time.time(), amended_harness=harness_commit())
    print(f"amended {t['ref']}: {meta['id']} now runs as wave {wave['wave']}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
