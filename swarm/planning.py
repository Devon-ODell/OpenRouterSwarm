"""Planner batches, goal-priority filtering and idle self-improvement.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import json, os, re, time, uuid

from learn import Ledger, format_playbook, json_array
from prompts import PERSONAS, PLANNER, PLANNER_SERVES, PRIORITIES, persona_text
from task_rules import goal_items, scope_gap

try:  # one class identity with the daemon's turn errors
    from .agent_turn import (CapReached, ModelError, NoCredits, ProviderDown, StepLimit)
except ImportError:  # pragma: no cover - script mode
    from agent_turn import (CapReached, ModelError, NoCredits, ProviderDown, StepLimit)

def serving_the_goal(tasks, items):
    """(tasks to queue, how many were dropped) for one planner batch.

    The judge rewarded a thumbnail validator 0.94 and a configurable word list 0.97 while the
    four deep games the goal puts first landed nothing, and those rewards are what taught the
    planner to keep proposing tooling. With no ranked priorities nothing is dropped: a goal that
    does not say what comes first cannot say a task serves nothing."""
    if not items:
        return tasks, 0

    def serves(t):
        try:
            n = int(t.get("serves"))
        except (TypeError, ValueError):
            return 0
        return n if 1 <= n <= len(items) else 0
    on_goal = [t for t in tasks if serves(t)]
    first = [t for t in on_goal if serves(t) == 1]
    # Half the batch has to be the first priority, so the rest is capped by how much of it
    # there is. No first-priority task means no batch: the planner is asked again, on another
    # model, rather than filling the queue with fifth things.
    rest = [t for t in on_goal if serves(t) != 1][:len(first)]
    kept = [t for t in tasks if t in first or t in rest]
    return kept, len(tasks) - len(kept)

def plan(c, q, budget, n=6, ledger=None, *, kinds, state, view_lock,
         all_resting, ensure_trunk, flint, git, journal, log, pool,
         read_goal, refresh_view, study, trunk_name):
    ledger = ledger or Ledger(state / "learn.json")
    ensure_trunk(c)
    goal = read_goal(c)
    persona = ledger.pick("persona", list(PERSONAS)) or "builder"
    model = ledger.pick("planner", pool(c, "planner"))
    if model is None:
        log(f"planner: {all_resting(ledger, c)}; will retry")
        return 0
    items = goal_items(goal)
    priorities = PRIORITIES.format(
        items="\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))) if items else ""
    serves_rule = PLANNER_SERVES if items else ""
    base, t = c.get("base_branch", "main"), trunk_name(c)
    _, landed = git(["log", "--format=- %s", "-n", "25", f"{base}..{t}"], cwd=c["repo"])
    landed = landed.replace("- swarm: ", "- ") or "- (nothing yet)"
    brk = "\n".join(f"- {b['title']}: {(b.get('scores') or {}).get('why', '')}"
                    for b in ledger.breakthroughs()) or "- (none yet)"
    seen = "\n".join(f"- {x}" for x in q.recent_titles(80)) or "- (nothing yet)"
    log(f"planning as {persona} with {model}")
    with view_lock:
        view = refresh_view(c)
        try:
            out = flint(PLANNER.format(
                goal=goal, priorities=priorities, serves_rule=serves_rule,
                serves_field=', "serves": 1' if items else "",
                persona_name=persona, persona=persona_text(persona, c), landed=landed,
                breakthroughs=brk, playbook=format_playbook(*ledger.playbook()),
                corpus=study(f"{goal[:600]} {persona}", c, k=3), seen=seen, n=n,
                test_cmd=c["test_cmd"], steps=max(2, c["steps"]["planner"] - 3)),
                view, c, "planner", "swarm", budget, c["steps"]["planner"], model)
        except (StepLimit, ModelError):
            ledger.update("planner", model, 0.0)  # ran out of steps or answered badly
            raise
    tasks = json_array(out)
    if tasks is None:
        ledger.update("planner", model, 0.0)
        raise ModelError(f"planner {model} returned no JSON task list")
    tasks = [t for t in tasks[:n] if isinstance(t, dict) and isinstance(t.get("title"), str)
             and isinstance(t.get("detail", ""), str) and t.get("kind", "feature") in kinds]
    kept, dropped = serving_the_goal(tasks, items)
    added = 0
    for task in kept:
        if q.add(task["title"], task.get("detail", ""), task.get("kind", "feature"),
                 persona=persona, planner_model=model, origin="plan",
                 serves=task.get("serves") if items else None):
            added += 1
    if dropped:
        log(f"planner: dropped {dropped} task(s) that did not serve the goal's priorities")
    log(f"planner queued {added} task(s)")
    journal("plan", added=added, dropped=dropped, persona=persona, model=model,
            priorities=len(items))
    return added

# A shift that runs out of queued work sleeps the rest of itself away. This fills that time
# and only that time: everything below stands down the moment real work exists.
IDLE_MIN_SECONDS = 1800       # less of the shift left than this and a new task cannot finish
IDLE_IMPROVEMENT = """There is no assigned work queued for this repository right now. Find ONE \
improvement worth making with the spare time, or say that there is none.

THE GOAL THIS REPOSITORY SERVES
{goal}

WHAT HAS LANDED RECENTLY
{landed}

ALREADY QUEUED, TRIED OR PROPOSED — do not repeat any of these
{seen}

WHAT COUNTS AS A GOOD ANSWER
This is spare time, not a mandate, and the bar is higher than for assigned work
because nobody asked for it. Propose something small, self-contained and
verifiable: a missing test for behaviour that already exists, an error path that
reports nothing a person could act on, a documented behaviour the code no longer
has, a crash on an input the code clearly meant to handle.

Do not propose a redesign, a new subsystem, a new dependency, a rename spanning
many files, or a change whose value is a matter of taste. If nothing clears that
bar, say so — "nothing worth doing" is a correct and useful answer, and a far
better one than inventing work.

Read the repository with your tools before you answer. Name real files.

Reply with ONE JSON object and nothing else. Either:

{{"title": "short imperative title", "kind": "feature|bugfix|test|refactor",
  "detail": "what to change and why, naming the files it touches",
  "acceptance": ["an observable that is true only once this is done", "..."],
  "why_now": "one sentence"}}

or, if there is nothing worth doing:

{{"title": null, "why_not": "one sentence"}}

Whatever you propose must be verifiable with: {test_cmd}
"""

def json_object(text):
    """The first JSON object in a reply, whether bare, fenced or buried in prose."""
    text = (text or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None

def idle_state(*, state_dir):
    """The persisted record of spare-time work: the cycles run and the one still open.

    Persisted because a restart mid-shift must resume this rather than start it again —
    the daemon restarts into edited source routinely, and an in-memory cycle counter would
    let one improvement be proposed, queued and forgotten several times over."""
    try:
        state = json.loads((state_dir / "idle-improvements.json").read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("cycles", [])
    state.setdefault("active", None)
    return state

def write_idle(state, *, state_dir):
    state["cycles"] = state.get("cycles", [])[-40:]
    tmp = state_dir / "idle-improvements.tmp"
    try:
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(state_dir / "idle-improvements.json")
    except OSError:
        pass                              # spare-time bookkeeping never fails a run

def settle_idle(state, q, *, read, log):
    """Close out the open improvement once its task has finished, and say how it went."""
    active = state.get("active")
    if not active:
        return None
    if any(r["id"] == active for r in q.pending()):
        return None                        # still queued or in flight
    row = next((r for r in reversed(read(q.done)) if r["id"] == active), None)
    state["active"] = None
    for cycle in reversed(state.get("cycles", [])):
        if cycle.get("queue_id") == active:
            cycle["status"] = (row or {}).get("status") or "vanished"
            cycle["outcome"] = ((row or {}).get("note") or "")[:300]
            log(f"idle improvement '{cycle.get('title', active)}' ended: {cycle['status']}")
            return cycle
    return None

def idle_ready(c, q, hours=None, *, hours_left, read_roadmap):
    """(ok, why not) — whether spare-time work may start at this moment.

    The order matters. Work somebody assigned always outranks work nobody asked for, and a
    roadmap blocker that needs a person is not made better by burying it under new tasks."""
    if q.ready():
        return False, "assigned work is ready to claim"
    pending = q.pending()
    if any(r.get("claimed") for r in pending):
        return False, "a task is in flight"
    if pending:
        return False, "queued work is waiting on its dependencies"
    roadmap = read_roadmap()
    if roadmap and roadmap.get("roots"):
        held = ", ".join(sorted(roadmap["roots"])[:4])
        return False, f"the roadmap is held on {held}, and that needs a person, not more tasks"
    if os.environ.get("FLINT_RUN_DEADLINE"):
        left = hours_left(hours)
        if left is None or left * 3600 < IDLE_MIN_SECONDS:
            return False, "too little of the shift is left for another task to finish"
    return True, ""

def idle_improvement(c, q, budget, ledger=None, hours=None, *, kinds,
                     state_dir, view_lock, can_take_a_turn, flint, git,
                     journal, log, pool, read_goal, refresh_view,
                     trunk_name, idle_state, write_idle, settle_idle,
                     idle_ready):
    """One self-directed improvement at a time, while there is genuinely nothing else to do.

    Returns the task queued, or None. Everything here is deliberately conservative: one
    improvement open at a time, a cooldown between cycles, the same allowance and the same
    shift deadline as any other turn, the lowest priority in the queue so a real task claimed
    a second later still goes first, and the proposal put through `scope_gap` before it is
    queued — spare-time work is exactly the kind that arrives vague."""
    ledger = ledger or Ledger(state_dir / "learn.json")
    state = idle_state()
    settle_idle(state, q)
    if state.get("active"):
        write_idle(state)
        return None                        # one at a time, always
    ok, why = idle_ready(c, q, hours)
    if not ok:
        if state.get("standing_down") != why:
            state["standing_down"] = why
            log(f"idle improvement standing down: {why}")
        write_idle(state)
        return None
    state.pop("standing_down", None)
    cooldown = max(300, int(c.get("idle_improvement_cooldown", 1800)))
    if time.time() - state.get("last_cycle", 0) < cooldown or not can_take_a_turn(c, budget):
        write_idle(state)
        return None
    model = ledger.pick("planner", pool(c, "planner"))
    if model is None:
        write_idle(state)
        return None
    base, t = c.get("base_branch", "main"), trunk_name(c)
    _, landed = git(["log", "--format=- %s", "-n", "15", f"{base}..{t}"], cwd=c["repo"])
    landed = landed.replace("- swarm: ", "- ") or "- (nothing yet)"
    seen = [*q.recent_titles(60), *(cy.get("title") for cy in state["cycles"] if cy.get("title"))]
    cycle = {"id": f"i{uuid.uuid4().hex[:8]}", "at": time.time(), "model": model}
    log(f"nothing queued; looking for one improvement with {model}")
    state["last_cycle"] = time.time()
    with view_lock:
        view = refresh_view(c)
        try:
            out = flint(IDLE_IMPROVEMENT.format(
                goal=read_goal(c), landed=landed,
                seen="\n".join(f"- {x}" for x in seen if x) or "- (nothing yet)",
                test_cmd=c["test_cmd"]),
                view, c, "planner", "swarm", budget,
                max(3, min(8, int(c.get("steps", {}).get("planner", 6)))), model)
        except (StepLimit, ModelError) as exc:
            cycle.update(status="no_answer", why=f"{type(exc).__name__}: {str(exc)[:200]}")
            state["cycles"].append(cycle)
            write_idle(state)
            log(f"idle improvement: {model} produced no usable answer ({type(exc).__name__})")
            return None
        except (CapReached, ProviderDown, NoCredits) as exc:
            # Not a failed cycle: nothing was asked. Try again after the usual cooldown.
            log(f"idle improvement deferred: {type(exc).__name__}")
            write_idle(state)
            return None
    proposal = json_object(out) or {}
    title = proposal.get("title")
    if not isinstance(title, str) or not title.strip():
        cycle.update(status="nothing_found",
                     why=str(proposal.get("why_not") or "no proposal in the reply")[:300])
        state["cycles"].append(cycle)
        write_idle(state)
        log(f"idle improvement: nothing worth doing — {cycle['why']}")
        journal("idle_improvement", status="nothing_found", model=model, why=cycle["why"])
        return None
    kind = proposal.get("kind") if proposal.get("kind") in kinds else "feature"
    acceptance = [a.strip() for a in (proposal.get("acceptance") or [])
                  if isinstance(a, str) and a.strip()][:8]
    detail = str(proposal.get("detail") or "").strip()
    if proposal.get("why_now"):
        detail += f"\n\nProposed as spare-time work because: {str(proposal['why_now'])[:300]}"
    candidate = {"title": title.strip(), "detail": detail, "acceptance": acceptance or [detail]}
    cycle["title"] = candidate["title"]
    gap = scope_gap(candidate, c)
    if gap:
        # Work nobody asked for is held to the same standard as work somebody did.
        cycle.update(status="rejected", why=gap)
        state["cycles"].append(cycle)
        write_idle(state)
        log(f"idle improvement: dropped '{candidate['title']}' before queueing — {gap}")
        journal("idle_improvement", status="rejected", model=model, title=candidate["title"],
                why=gap)
        return None
    task = q.add(candidate["title"], candidate["detail"], kind, origin="idle",
                 planner_model=model, acceptance=candidate["acceptance"], priority=-1)
    if not task:
        cycle.update(status="duplicate", why="already queued or the queue is full")
        state["cycles"].append(cycle)
        write_idle(state)
        return None
    cycle.update(status="queued", queue_id=task["id"], kind=kind)
    state["cycles"].append(cycle)
    state["active"] = task["id"]
    write_idle(state)
    log(f"idle improvement queued {task['id']}: {task['title']}")
    journal("idle_improvement", status="queued", model=model, id=task["id"],
            title=task["title"], kind=kind)
    return task
