"""Timed, unattended Flint cycles. No model calls are made by the supervisor."""
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


TRIGGER = re.compile(
    r"^(?:run this nonstop|/nonstop)\b"
    r"(?:\s+for\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+|inf|nan))\s*(?:hours?|h)\b)?"
    r"(?:\s*:\s*|\s+|$)(.*)$", re.IGNORECASE | re.DOTALL)


def parse_trigger(text):
    """Only explicit command prefixes start unattended work, never quoted prose."""
    match = TRIGGER.fullmatch(text.strip())
    if not match:
        return None
    hours, goal = match.groups()
    return (float(hours) if hours is not None else None, goal.strip())


def validate_hours(hours):
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a finite positive number")
    return hours


def resolve_goal(goal, cwd, previous=""):
    goal = goal.strip() or previous.strip()
    if not goal:
        path = Path(cwd) / "GOAL.md"
        if path.is_file():
            goal = path.read_text().strip()
    if not goal:
        raise ValueError("Give nonstop mode a goal, or create GOAL.md in the target directory.")
    return goal


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def save_checkpoint(path, messages):
    # Keep recent evidence without carrying a growing conversation forever.
    # This is continuity material, not proof of completion or a resumable transcript.
    recent = [{"role": m["role"], "content": str(m.get("content", ""))[-1600:],
               "tool_calls": [{"name": c["function"]["name"],
                               "arguments": c["function"]["arguments"][:1000]}
                              for c in m.get("tool_calls", [])[:6]]}
              for m in messages[-8:] if m["role"] in ("assistant", "tool")]
    while len(json.dumps(recent)) > 14000 and len(recent) > 1:
        recent.pop(0)
    write_json(path, recent)


def tail(path, size=10000):
    if not Path(path).is_file():
        return ""
    with open(path, "rb") as file:
        file.seek(0, os.SEEK_END)
        file.seek(max(0, file.tell() - size))
        return file.read().decode("utf-8", "replace")


def run_process(command, cwd, out_path, err_path, timeout, prompt=None):
    """Bound a worker/test process and clean up its ordinary shell descendants."""
    with open(out_path, "w") as out, open(err_path, "w") as err:
        p = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err,
                             stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                             text=True, start_new_session=True)
        try:
            p.communicate(input=prompt, timeout=max(0.001, timeout))
            return p.returncode
        except subprocess.TimeoutExpired:
            return 124
        finally:
            # Also stop background children left behind after a successful cycle.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:      # already gone, or the OS refused: neither is worth crashing on
                pass
            p.wait()


def cycle_prompt(goal, carry, test_command, cycle):
    return f"""You are in unattended work cycle {cycle} on this codebase.

USER'S GOAL (keep this scope throughout the run):
{goal}

RECENT PROGRESS / TOOL EVIDENCE (may be partial; verify against disk):
{carry or '(First cycle: inspect the current code and tests.)'}

Inspect current files before editing; other people may be working here.
Choose one concrete unfinished part of the goal, implement it, and verify it.
Continue existing work rather than recreating completed features. If the goal
appears complete, check its acceptance criteria and investigate actual defects;
do not invent unrelated features or churn working code merely to fill time.
Do not repeat a failing approach without new evidence. Preserve other people's
changes. Do not commit, reset, stash, switch branches, deploy, or publish.
Verification command: {test_command or 'discover and run the relevant tests/build; report missing coverage honestly'}

End with a compact handoff: changes made, exact test results, unresolved issues,
and the next useful step. A claim of success is not a substitute for test evidence.
The supervisor controls the clock; finish this focused cycle instead of sleeping
or launching background work to simulate the remaining hours.
"""


def run_nonstop(goal, cwd, model, state_dir, hours=8, test_command=None):
    """Auto-approve serial work cycles until the deadline; retain logs and files."""
    hours = validate_hours(hours)
    cwd = Path(cwd).resolve()
    goal = resolve_goal(goal, cwd)
    root = Path(state_dir) / "nonstop"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
    with open(root / f"{key}.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A nonstop run already owns this codebase; stop it before starting another.")
        return _run(goal, cwd, model, Path(state_dir), root, hours, test_command)


def _run(goal, cwd, model, state_dir, root, hours, test_command):
    session = root / (dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    session.mkdir(mode=0o700)
    deadline = time.monotonic() + hours * 3600
    state = {"goal": goal, "cwd": str(cwd), "model": model, "hours": hours,
             "test_command": test_command, "status": "running", "cycle": 0,
             "deadline": time.time() + hours * 3600}
    carry, failure, previous, repeated, errors, unclassified = "", "", None, 0, 0, 0

    def event(message, **fields):
        print(f"nonstop: {message}", file=sys.stderr, flush=True)
        with open(session / "events.jsonl", "a") as file:
            file.write(json.dumps({"time": time.time(), "message": message, **fields}) + "\n")
        state.update(fields)
        write_json(session / "status.json", state)

    def pause(seconds):
        end = min(deadline, time.monotonic() + max(0, seconds))
        while time.monotonic() < end:
            time.sleep(max(0, min(30, end - time.monotonic())))

    def interrupt(*_):
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        event(f"running for up to {hours:g} hours in {cwd}; edits and shell commands are enabled")
        event(f"logs and progress: {session}")
        while time.monotonic() < deadline:
            cycle = state["cycle"] + 1
            prefix = session / f"cycle-{cycle:04d}"
            out, err, checkpoint = (prefix.with_suffix(s) for s in (".out.log", ".err.log", ".json"))
            event(f"cycle {cycle} starting; progress: {err}", cycle=cycle)
            rc = run_process(
                [sys.executable, str(Path(__file__).with_name("flint.py")),
                 "-C", str(cwd), "-m", model, "--yolo", "-p", "-",
                 "--checkpoint", str(checkpoint)], cwd, out, err,
                min(1800, deadline - time.monotonic()),
                prompt=cycle_prompt(goal, carry + failure, test_command, cycle))
            summary = tail(out, 6000)
            evidence = tail(checkpoint, 16000)
            if evidence.strip() == "[]":
                evidence = ""
            # A failed request before any work must not erase the last useful handoff.
            if summary or evidence:
                carry = f"Worker exit: {rc}\nSummary:\n{summary}\nRecent tool evidence:\n{evidence}"
            event(f"cycle {cycle} ended (exit {rc})", worker_exit=rc)
            if time.monotonic() >= deadline:
                break
            if rc == 4:
                event("stopped: account/credit limit needs attention", status="blocked")
                return 4
            if rc in (3, 6):
                errors = unclassified = 0
                delay = 60
                if rc == 3:
                    try:
                        counter = json.loads((state_dir / "requests.json").read_text())
                        delay = max(60, counter.get("blocked_until", time.time() + 60) - time.time())
                    except (OSError, ValueError):
                        pass
                event(f"quota pause for up to {delay:.0f}s; the run deadline still applies")
                pause(delay)
                continue
            if rc not in (0, 5):
                errors += 1
                unclassified = unclassified + 1 if rc == 1 else 0
                failure = "\nLast worker error:\n" + tail(err, 2000)
                if unclassified >= 3:
                    event("stopped after three consecutive worker errors; inspect logs", status="failed")
                    return 1
                delay = min(900, 30 * 2 ** min(errors - 1, 5))
                event(f"worker unavailable or timed out; retrying in {delay}s")
                pause(delay)
                continue
            errors = unclassified = 0
            failure = ""
            if test_command:
                test_out, test_err = prefix.with_suffix(".test.log"), prefix.with_suffix(".test.err.log")
                test_rc = run_process([os.environ.get("SHELL") or "/bin/sh", "-c", test_command],
                                      cwd, test_out, test_err, min(900, deadline - time.monotonic()))
                carry += f"\nSUPERVISOR TEST: {test_command}\nExit: {test_rc}\n" + tail(test_out, 3000) + tail(test_err, 3000)
                event(f"verification ended (exit {test_rc})", test_exit=test_rc, test_cycle=cycle)
            repeated = repeated + 1 if summary and summary == previous else 0
            previous = summary
            if repeated >= 2:
                carry += "\nYour summary repeated across cycles. Re-read evidence, change approach, and avoid repeated no-op edits."
                event("repeated summaries; pausing five minutes before rechecking")
            pause(300 if repeated >= 2 else 15)
        event("time limit reached; changes retained for review", status="time_limit")
        return 0
    except KeyboardInterrupt:
        event("interrupted; changes and progress retained", status="interrupted")
        return 130
    except Exception as exc:
        event(f"stopped: {type(exc).__name__}: {exc}", status="failed")
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
