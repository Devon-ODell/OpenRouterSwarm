"""Roadmap dispatch: queue packets whose gates are open, and say what blocks the rest.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import datetime as dt, json, re
from pathlib import Path

# What holds a roadmap packet, and the one thing that would release it. A blocker with no
# stated next action is the same as no blocker at all: the run stalls and the report says
# only that it stalled.
BLOCKER_ACTIONS = {
    "dependency": "land the packet(s) it waits on, or repoint this packet's depends_on",
    "coordinator_gate": "record the gate's evidence in docs/roadmap/EXECUTION.json and commit it",
    "coordinator_owned": "this packet is the coordinator's to accept; record it in "
                         "EXECUTION.json under packets.<id> with its evidence",
    "attempt_failed": "read the attempt's evidence, then record packets.<id>.retry "
                      "{reason, at} in EXECUTION.json to authorise another run",
    "unverified_completion": "the queue says done but no integrated commit for it is on trunk; "
                             "record the accepted commit, or packets.<id>.retry to run it again",
    "queue_full": "nothing — it is next in line once the queue drains",
}


def dispatch_roadmap(c, q, *, state, read, git, journal, log, retry_recorded, trunk_name, said):
    """Dispatch only committed packets whose dependencies and owner gates are evidenced.

    The coordinator ledger stays authoritative for human gates. Live queue/commit
    evidence is reported separately so workers cannot accept their own gates.

    Every packet that is neither accepted nor in flight is written to the execution report
    with the reason it is held and the one thing that would release it. A packet whose
    attempt failed used to be skipped by a bare `title in history` test: no blocker, no log
    line, no queue entry. A roadmap that had stopped moving was indistinguishable from a
    roadmap with nothing left to do."""
    repo, trunk = Path(c["repo"]), trunk_name(c)

    def committed(path):
        _, raw = git(["show", f"{trunk}:{path}"], cwd=repo, check=True)
        return json.loads(raw)

    rows = committed(c["roadmap_file"])["tasks"]
    ledger = committed("docs/roadmap/EXECUTION.json")
    if ledger.get("implementation_started") is not True:
        raise ValueError("roadmap implementation has not been activated")
    with q.locked():
        pending, done = read(q.path), read(q.done)
    titles = {p["id"]: p.get("title", p["id"]) for p in rows}
    of_title = {p.get("title"): p["id"] for p in rows}
    # Every queue row a packet has produced, oldest first. Rows queued before packets carried
    # their id are matched on title, which is all they were ever keyed by.
    tries = {p["id"]: [] for p in rows}
    live = {r["id"] for r in pending}
    for r in sorted(done + pending, key=lambda r: r.get("created", 0)):
        pid = r.get("packet") or of_title.get(r.get("title"))
        if pid in tries:
            tries[pid].append(r)
    accepted = {}
    for packet in rows:
        pid = packet["id"]
        evidence = ledger.get("packets", {}).get(pid, {})
        commit = evidence.get("accepted_commit")
        if evidence.get("status") == "accepted" and evidence.get("evidence"):
            if packet["dispatch"] == "coordinator":
                accepted[pid] = evidence
            elif commit and git(["merge-base", "--is-ancestor", commit, trunk], cwd=repo)[0] == 0:
                accepted[pid] = evidence
        for queued in tries[pid]:
            match = re.search(r"integrated ([0-9a-f]{40}) on", queued.get("note") or "")
            if queued.get("status") == "done" and match:
                commit = match.group(1)
                if git(["merge-base", "--is-ancestor", commit, trunk], cwd=repo)[0] == 0:
                    accepted[pid] = {"status": "accepted", "queue_id": queued["id"],
                                     "accepted_commit": commit, "evidence": queued["note"]}
    added, blocked, flight = [], {}, []

    def hold(pid, reason, waiting_on=(), **extra):
        blocked[pid] = {"title": titles.get(pid, pid), "reason": reason,
                        "waiting_on": list(waiting_on),
                        "next_action": BLOCKER_ACTIONS[reason], **extra}

    for packet in rows:
        pid = packet["id"]
        if pid in accepted:
            continue
        if packet["dispatch"] != "swarm":
            hold(pid, "coordinator_owned")
            continue
        attempt = tries[pid][-1] if tries[pid] else None
        if attempt and attempt["id"] in live:
            flight.append({"queue_id": attempt["id"], "packet": pid, "title": attempt["title"],
                           "claimed": bool(attempt.get("claimed"))})
            continue
        missing = [dep for dep in packet.get("depends_on", []) if dep not in accepted]
        gates = [g for g in packet.get("coordinator_gates", [])
                 if not ledger.get("gates", {}).get(g, {}).get("evidence")]
        if missing:
            hold(pid, "dependency", missing)
            continue
        if gates:
            hold(pid, "coordinator_gate", gates)
            continue
        if attempt and not retry_recorded(ledger.get("packets", {}).get(pid, {}),
                                          attempt.get("finished") or attempt.get("created")):
            hold(pid, "unverified_completion" if attempt.get("status") == "done"
                 else "attempt_failed",
                 queue_id=attempt["id"], attempt_status=attempt.get("status"),
                 attempt_note=(attempt.get("note") or "")[:400], attempts=len(tries[pid]))
            continue
        if len(pending) + len(added) >= 5:
            hold(pid, "queue_full", queued=len(pending) + len(added))
            continue
        title = packet["title"] if not tries[pid] else f"{packet['title']} (retry {len(tries[pid]) + 1})"
        detail = (packet["detail"] + "\n\nREAD: docs/roadmap/README.md and this packet in "
                  + c["roadmap_file"] + "\nVERIFICATION:\n" + "\n".join(packet.get("commands", [])))
        task = q.add(title, detail, "feature", origin="roadmap", packet=pid,
                     priority=packet["priority"], execution_class=packet["execution_class"],
                     acceptance=packet["acceptance"], serves=1)
        if task:
            added.append(task)
            log(f"roadmap {pid}: queued {task['id']}")
    downstream = {p["id"]: [] for p in rows}
    for p in rows:
        for dep in p.get("depends_on", []):
            if dep in downstream:
                downstream[dep].append(p["id"])
    moving = {r["packet"] for r in flight} | {t.get("packet") for t in added}
    for pid, row in blocked.items():
        row["blocks"] = sorted(downstream.get(pid, []))
        # A root blocker waits on nothing that is already blocked or already being worked on.
        # Those are the ones a person has to act on; everything else resolves on its own once
        # they do, and listing it alongside them only buries the one that matters.
        row["root"] = not any(dep in blocked or dep in moving for dep in row["waiting_on"])
    roots = {pid: row for pid, row in blocked.items() if row["root"]}
    mark = json.dumps(sorted((pid, row["reason"]) for pid, row in roots.items()))
    if roots and mark != said.get("blockers"):
        said["blockers"] = mark
        for pid, row in sorted(roots.items()):
            log(f"roadmap {pid} ({row['title']}) is held — {row['reason']}: {row['next_action']}")
        journal("roadmap_blocked", roots=sorted(roots),
                reasons={pid: row["reason"] for pid, row in roots.items()})
    elif not roots:
        said.pop("blockers", None)
    report = {"updated": dt.datetime.now().isoformat(), "accepted": accepted,
              "blocked": blocked, "roots": sorted(roots), "in_flight": flight,
              "pending": [{"id": t["id"], "title": t["title"]} for t in pending + added]}
    tmp = state / "roadmap-execution.tmp"
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(state / "roadmap-execution.json")
    return added


def read_roadmap(*, state):
    """The last roadmap execution report, for `swarm status` and the panel."""
    try:
        return json.loads((state / "roadmap-execution.json").read_text())
    except (OSError, ValueError):
        return None