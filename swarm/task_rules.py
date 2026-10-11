"""Task rules: scope, failure classification, goal priorities, request estimates.

Extracted from swarmd (P2). Pure policy: every function is a pure function of its
parameters; no module reads swarmd globals.
"""
import re

PRIORITY_HEADING = re.compile(r"^#{1,6}\s*.*(?:work on now|priorities).*$", re.I | re.M)
NUMBERED = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
# How much of a landed change's reward its place in that list is worth. The judge gave 0.94 to
# a thumbnail validator and 0.97 to a configurable word list while the four deep games GOAL.md
# puts first landed nothing, and those rewards are what trained the planner toward tooling.
GOAL_ITEM_WEIGHT = {1: 1.0, 2: 0.85, 3: 0.6, 4: 0.5, 0: 0.35}


def goal_items(goal_text):
    """The numbered priorities under the goal's own "work on now" heading, in order.

    Empty when the goal does not rank anything, and then nothing downstream changes: no task is
    dropped for serving no item and no reward is scaled. A goal that does not say what comes
    first cannot be used to say a change came second."""
    m = PRIORITY_HEADING.search(goal_text or "")
    if not m:
        return []
    items, seen = [], set()
    for line in (goal_text[m.end():]).splitlines():
        if line.startswith("#"):
            break                       # the next heading ends the list
        row = NUMBERED.match(line)
        if row:
            n = int(row.group(1))
            if n in seen:
                break                   # a second list under the same heading
            seen.add(n)
            items.append(re.sub(r"\*\*|`", "", row.group(2)).strip()[:200])
    return items


def goal_item_weight(n, items):
    """What a landed change's place in the priority list multiplies its reward by."""
    if not items:
        return 1.0
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= len(items):
        n = 0
    return GOAL_ITEM_WEIGHT.get(n, 0.5 if n else GOAL_ITEM_WEIGHT[0])



STATIC_REQUEST_ESTIMATES = {
    "bugfix": 25,
    "feature": 35,
    "test": 20,
    "documentation": 12,
    "refactor": 30,
}


def estimate_requests(r):
    """Estimated request cost of a task, for EV ranking."""
    cls = str((r or {}).get("request_class") or "").lower()
    if cls in ("small",):
        return 15
    if cls == "medium":
        return 30
    if cls == "manual":
        return 60
    kind = (r or {}).get("kind") or "feature"
    if kind in STATIC_REQUEST_ESTIMATES:
        return STATIC_REQUEST_ESTIMATES[kind]
    return 45



# ------------------------------------------------------------------ failure classes

# Whose problem a failure is. Queue.release treated them all alike — a note, an attempt spent,
# and after two attempts a split or a park — so a step limit, a sandbox timeout or a restart
# cost a task one of its two lives and scored the model down for it. 90 of 121 finished tasks
# were split or parked, every deep-game milestone among them.
TASK_STAGES = frozenset({"tests_failed", "rejected", "weakened_tests", "out_of_scope"})

# A path, or a symbol, written plainly enough that an implementer could open it.
NAMES_A_PATH = re.compile(
    r"[\w./-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|md|html|css|scss|sh|go|rs|java|rb|"
    r"c|h|cc|cpp|hpp|yml|yaml|toml|sql|swift|kt)\b")
NAMES_A_SYMBOL = re.compile(r"\b(?:def|class|function|const|fn|func|struct|interface)\s+\w+"
                            r"|\b\w+\(\)|`[^`\n]{2,60}`")
# Phrasing with no wrong answer. On its own this is not a defect — plenty of good tasks say
# "and so on" in passing — so it only counts against a task that also names no observable.
BROAD_ASK = re.compile(
    r"\b(?:everything|anything else|all (?:the )?(?:code|files|tests|games|issues)|"
    r"as (?:needed|appropriate|you see fit)|and so on|etc\.?|various (?:issues|places|files)|"
    r"wherever (?:needed|possible|appropriate)|"
    r"(?:improve|clean up|tidy|refactor|fix) (?:the )?(?:whole|entire|rest of the|codebase)|"
    r"any (?:other )?(?:issues|bugs|problems)(?: you (?:can )?find)?)\b", re.I)


def scope_gap(task, c=None):
    """Why this task cannot be handed to an implementer as written, or "" when it can.

    A roadmap packet arrives with acceptance criteria, verification commands and the files it
    concerns, and `dispatch_roadmap` will not hand it out until its gates are evidenced. Work
    that arrives any other way — a planner batch, a question the editor turned into a task, a
    report typed in by hand — reaches the same implementer with whatever text it happened to
    carry, and bypasses every one of those checks. A request broad enough to have no wrong
    answer then spends an implementer turn, a full suite, a review and a repair before anyone
    discovers it had no scope. This applies the packet's guarantees to every task, before a
    model is paid to find the gap.

    It is deliberately hard to fail: a task is refused only when it declared acceptance
    criteria that say no more than its title does, and nothing in it names a file, a symbol
    or a command either — or when it reads as open-ended however it arrived."""
    title = (task.get("title") or "").strip()
    text = f"{title}\n{task.get('detail') or ''}"
    declared = task.get("acceptance")
    rows = [r for r in (declared or []) if isinstance(r, str) and r.strip()]
    # Work order §10: a task marked manual is held before a model request — queued for
    # visibility but never automatically claimed; a person must pick it up.
    if (task.get("request_class") == "manual" or task.get("origin") == "manual"):
        return ("manual task: queued for visibility but not automatically claimed; "
                "a person must pick it up")
    # Every task is verified by the configured test_cmd; a per-task verifier is
    # additive, not required. What is required is that the task name a file, a symbol
    # or an observable so a finished attempt can be told from an abandoned one — the
    # existing checks below. Open-ended epics are what this gate holds.
    # A criterion counts only if it says more than the title did and is not itself an
    # open-ended ask. The editor copies a task's detail into its acceptance, so "clean up the
    # whole codebase as needed" would otherwise arrive as its own acceptance criterion and
    # pass a check meant to catch exactly that.
    if [r for r in rows if r.strip().lower() != title.lower() and not BROAD_ASK.search(r)]:
        return ""
    broad = BROAD_ASK.search(text)
    if broad:
        return (f"open-ended request ({broad.group(0).strip()!r}) with no acceptance criterion "
                "beyond its title: say what must be true when it is done")
    if declared is None:
        # Nothing was declared at all, so there is no vacuous declaration to object to. The
        # queue's own `criteria()` is what decides whether a task may be added; this gate
        # judges what a task claims about itself, not what it omitted.
        return ""
    if not (NAMES_A_PATH.search(text) or NAMES_A_SYMBOL.search(text)):
        return ("nothing here names a file, a symbol or an observable, so there is no way to "
                "tell a finished attempt from an abandoned one: add an acceptance criterion "
                "or name the code it concerns")
    return ""
# Not the model answering badly: the baseline was already broken, trunk moved under the attempt,
# the supervisor declined it, or the run was stopped.

HARNESS_STAGES = frozenset({"baseline", "conflict", "refused", "interrupted", "deferred",
                            "agent_timeout", "request_budget", "model_pool_exhausted",
                            "exploration_exhausted"})
HARNESS_NOTE = re.compile(
    r"step limit|stopped after \d+ rounds|timed out|timeout|exceeded \d+s|sandbox|"
    r"interrupted before completion|deferred|killed|sigkill|provider (?:unavailable|kept failing)|"
    r"not available|empty response|connection|\b50\d\b|\b429\b", re.I)
MODEL_NOTE = re.compile(r"no usable json|malformed|unparseable|could not parse|invalid json|"
                        r"wrote .*as (?:reply )?text|did not follow", re.I)


def failure_class(stage, note="", edited=None):
    """`task`, `model` or `harness` — the only distinction worth acting on differently.

    task     the change was wrong: the tests failed, or the reviewer proved a defect.
    model    the model did not produce usable output, or edited and still changed nothing.
    harness  this swarm lost the turn: rounds, wall clock, a restart, a sandbox, a provider.
    """
    note = str(note or "")
    if stage == "review_format_error":
        return "review_format"
    if stage in TASK_STAGES:
        return "task"
    if stage in HARNESS_STAGES:
        return "harness"
    if stage == "model_error":
        # A provider that returned 5xx or nothing at all is not a model answering badly.
        return "harness" if HARNESS_NOTE.search(note) else "model"
    if stage == "review_error":
        return "harness" if HARNESS_NOTE.search(note) else "model"
    if stage == "no_change":
        # Having tried an edit and still changed nothing is the model's doing. Never reaching an
        # edit is this harness running the rounds out on it.
        return "model" if edited else "harness"
    if MODEL_NOTE.search(note):
        return "model"
    if HARNESS_NOTE.search(note):
        return "harness"
    return "task"



# ------------------------------------------------------------------ earlier attempts

# The one line of a failure that says what to do differently.
FAILING = re.compile(r"^(?:FAIL|ERROR):.*|^E\s{2,}.*|^\s*(?:Assertion|Type|Value|Name|Attribute|"
                     r"Index|Key|Import|Module|Syntax|Runtime|OS|IO)Error\b.*|^\s*assert\s.*",
                     re.M)


def first_failing_line(text):
    """The first line of a test failure worth repeating, or ""."""
    m = FAILING.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip()[:200] if m else ""


# A turn's progress lines: what it did, not what went wrong.
PROGRESS_LINE = re.compile(r"^(?:round \d+/\d+: |tool: |provider busy |step limit: )")
# Lines that say what went wrong, wherever they are in a failure's text.
SIGNAL_LINE = re.compile(r"\berror\b|\bfail(?:s|ed|ure)?\b|assert|panicked|evaluates to|"
                         r"stopped after|^flint:|traceback|exception", re.I)


def condense(text, limit=800):
    """A failure's text in `limit` characters, what went wrong first.

    Notes used to be cut to their head or their tail. On SkyRing the head of an exploration
    note was "round 1/10: requesting …" and the tail of a gate note was a build log
    ("Bundling source/chr/…"), while the lines that mattered ("Stopped after 8 rounds with no
    edit", "evaluates to '44 == 36'") were cut. This keeps the first line, a count of the
    turn's rounds and tools, and every line that names a failure, then fills with the tail."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    # Gate evidence arrives JSON-escaped, its newlines as a literal backslash-n.
    lines = [l.strip() for l in re.split(r"\n|\\n", text) if l.strip()]
    rounds = sum(1 for l in lines if re.match(r"round \d+/\d+: ", l))
    tools = {}
    for l in lines:
        if l.startswith("tool: "):
            tools[l[6:].strip()] = tools.get(l[6:].strip(), 0) + 1
    head = []
    if rounds:
        head.append(f"{rounds} rounds; tools: "
                    + (", ".join(f"{t} x{n}" for t, n in tools.items()) or "none"))
    rest = [l for l in lines if not PROGRESS_LINE.match(l)]
    for l in rest[:1] + [l for l in rest[1:] if SIGNAL_LINE.search(l)]:
        # An absolute path says where the gate ran, not what failed: keep the file's name.
        l = re.sub(r"(?:evidence: )?(?:/[\w.@+-]+){2,}/([\w.+-]+)", r"\1", l)[:240]
        if l not in head:
            head.append(l)
    out = "\n".join(head)
    if len(out) >= limit:
        return out[:limit]
    room = limit - len(out) - 3
    tail = text[-room:].split("\n", 1)[-1] if room > 80 else ""
    news = [l.strip() for l in re.split(r"\n|\\n", tail)
            if l.strip() and not PROGRESS_LINE.match(l.strip()) and l.strip() not in head]
    return out + ("\n…\n" + tail if news else "")



