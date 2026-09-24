#!/usr/bin/env python3
"""
flint — a tiny terminal coding agent that runs on free OpenRouter models
(default: inclusionAI Ling 3.0 Flash Fin).

    # Set OPENROUTER_API_KEY=sk-or-... in .env beside this script, or:
    export OPENROUTER_API_KEY=sk-or-...
    python flint.py                    # interactive
    python flint.py -p "fix the tests" # one-shot, prints the answer, exits
"""
import argparse
import datetime
import difflib
import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request
from dotenv import load_dotenv
from openai import APIConnectionError, APIError, OpenAI, RateLimitError
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

try:
    import readline  # noqa: F401  (arrow keys + history in input())
except ImportError:
    pass
try:
    import fcntl  # cross-process lock for the shared request budget (macOS/Linux)
except ImportError:
    fcntl = None

# ─── config ──────────────────────────────────────────────────────────────────
# Read the .env beside this script, even when launched from another directory.
# Explicitly exported environment variables take precedence. Swarm workers run
# in a sandbox that denies reading .env; they receive the key via environment.
try:
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
except OSError:
    pass

DEFAULT_MODEL = os.environ.get("FLINT_MODEL", "inclusionai/ling-3.0-flash-fin:free")
FALLBACKS = [m.strip() for m in os.environ.get("FLINT_FALLBACKS", "").split(",") if m.strip()]
MAX_STEPS = int(os.environ.get("FLINT_MAX_STEPS", "40"))  # tool rounds per turn
MAX_TOOL_OUTPUT = 20_000                                   # chars sent back to the model
CONTEXT_WARN_TOKENS = 200_000
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
                ".pytest_cache", "dist", "build", ".next", ".idea", ".DS_Store", "target"}
NEEDS_APPROVAL = {"write_file", "edit_file", "bash"}
STATE_DIR = Path(os.environ.get("FLINT_HOME", "~/.flint")).expanduser()
RPM_LIMIT = int(os.environ.get("FLINT_RPM", "18"))  # OpenRouter caps :free models at 20/min
OPENROUTER = "https://openrouter.ai/api/v1"
CORPUS_DB = Path(os.environ.get("FLINT_CORPUS_DB", "~/.flint/corpus.db")).expanduser()
# Headless shell commands do not inherit credentials from the environment.
SECRET_ENV = re.compile(r"API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL", re.I)

ACCENT = "cyan"
console = Console(highlight=False)


# ─── tools ───────────────────────────────────────────────────────────────────
def _p(path):
    return Path(path).expanduser()


def _truncate(s, limit=MAX_TOOL_OUTPUT):
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n\n… [{len(s) - limit} chars truncated] …\n\n{s[-half:]}"


def read_file(path, offset=1, limit=400):
    p = _p(path)
    if not p.exists():
        return f"Error: {path} does not exist."
    if p.is_dir():
        return f"Error: {path} is a directory. Use list_files."
    if b"\x00" in p.read_bytes()[:8192]:
        return f"{path} looks like a binary file; not shown."
    lines = p.read_text(errors="replace").splitlines()
    if not lines:
        return "(empty file)"
    start = max(1, int(offset))
    end = min(len(lines), start + int(limit) - 1)
    body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
    if start > 1 or end < len(lines):
        body += f"\n[showing lines {start}-{end} of {len(lines)}]"
    return body


def write_file(path, content):
    p = _p(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content)
    return f"{'Overwrote' if existed else 'Created'} {path} ({len(content.splitlines())} lines)."


def _plan_edit(path, old_str, new_str, replace_all=False):
    """Returns (old_text, new_text, error)."""
    p = _p(path)
    if not p.exists():
        return None, None, f"Error: {path} does not exist. Use write_file to create it."
    text = p.read_text(errors="replace")
    n = text.count(old_str)
    if n == 0:
        return None, None, ("Error: old_str not found. It must match the file exactly, "
                            "including whitespace and indentation. Re-read the file and retry.")
    if n > 1 and not replace_all:
        return None, None, (f"Error: old_str appears {n} times. Include more surrounding "
                            "lines to make it unique, or set replace_all=true.")
    new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
    return text, new_text, None


def edit_file(path, old_str, new_str, replace_all=False):
    old, new, err = _plan_edit(path, old_str, new_str, replace_all)
    if err:
        return err
    _p(path).write_text(new)
    n = old.count(old_str) if replace_all else 1
    return f"Edited {path} ({n} replacement{'s' if n != 1 else ''})."


def list_files(pattern="**/*", path="."):
    root = _p(path)
    if not root.is_dir():
        return f"Error: {path} is not a directory."
    pat2 = pattern[3:] if pattern.startswith("**/") else pattern
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for f in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pat2):
                out.append(rel)
                if len(out) >= 300:
                    return "\n".join(out) + "\n[truncated at 300 files — narrow the pattern]"
    return "\n".join(out) or "No files matched."


def search(pattern, path=".", glob=None):
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color", "never", "-e", pattern]
        if glob:
            cmd += ["--glob", glob]
        cmd.append(path)
        r = subprocess.run(cmd, capture_output=True, text=True)
        lines = r.stdout.splitlines()
    else:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"Error: bad regex ({e})"
        lines = []
        for rel in list_files(glob or "**/*", path).splitlines():
            fp = _p(path) / rel
            try:
                for i, line in enumerate(fp.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        lines.append(f"{os.path.join(path, rel)}:{i}:{line}")
            except (OSError, UnicodeDecodeError):
                continue
            if len(lines) > 200:
                break
    if not lines:
        return "No matches."
    extra = f"\n[{len(lines) - 200} more matches not shown]" if len(lines) > 200 else ""
    return "\n".join(lines[:200]) + extra


def bash(command, timeout=120):
    shell = os.environ.get("SHELL") or "/bin/sh"
    env = None
    if os.environ.get("FLINT_HEADLESS"):
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    try:
        r = subprocess.run(command, shell=True, executable=shell, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=int(timeout), env=env)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    out = r.stdout or ""
    if r.stderr:
        out += ("\n" if out else "") + "[stderr]\n" + r.stderr
    return f"exit code {r.returncode}\n{out.strip() or '(no output)'}"


def study(query, k=5):
    """Search the local study corpus: MIT OCW lecture cards first, then source pages, with
    absolute paths a read_file call can open. Local BM25 lookup; costs no API quota."""
    try:
        from swarm import mit_corpus
        text = mit_corpus.study(query, max(1, min(int(k), 10)), db=CORPUS_DB)
    except Exception as e:
        return f"Error: study corpus unavailable ({type(e).__name__}: {e})"
    return text or "No matches in the study corpus. Try different or more specific terms."


TOOLS = {"read_file": read_file, "write_file": write_file, "edit_file": edit_file,
         "list_files": list_files, "search": search, "bash": bash, "study": study}


def _schema(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMAS = [
    _schema("read_file", "Read a text file. Returns numbered lines. Use offset/limit for big files.",
            {"path": {"type": "string"},
             "offset": {"type": "integer", "description": "1-based first line (default 1)"},
             "limit": {"type": "integer", "description": "max lines (default 400)"}}, ["path"]),
    _schema("write_file", "Create a file or fully overwrite one. Prefer edit_file for changes.",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _schema("edit_file", "Replace old_str with new_str in a file. old_str must match exactly "
                         "(whitespace included) and be unique unless replace_all is true. "
                         "Always read_file first.",
            {"path": {"type": "string"}, "old_str": {"type": "string"},
             "new_str": {"type": "string"}, "replace_all": {"type": "boolean"}},
            ["path", "old_str", "new_str"]),
    _schema("list_files", "List files matching a glob like '**/*.py' (skips .git, node_modules, venvs).",
            {"pattern": {"type": "string"}, "path": {"type": "string"}}, []),
    _schema("search", "Regex search file contents. Returns path:line:text.",
            {"pattern": {"type": "string"}, "path": {"type": "string"},
             "glob": {"type": "string", "description": "optional file filter, e.g. '*.go'"}},
            ["pattern"]),
    _schema("bash", "Run a shell command in the project directory. Each call is a fresh shell, "
                    "so chain with && if you need cd. Use for tests, git, builds, installs.",
            {"command": {"type": "string"},
             "timeout": {"type": "integer", "description": "seconds (default 120)"}}, ["command"]),
    _schema("study", "Search the local study corpus (MIT OCW lecture cards, slides, notes, problem sets "
                     "and transcripts on deep learning and machine learning, probability, matrix calculus, discrete math and combinatorics, algorithms and Python, mathematical finance, fintech, blockchain, risk and decision analysis, venture finance, microeconomics, game theory, public finance, perception and psychology; plus past code reviews) for techniques "
                     "and reference material. Hits under flint/cards/ name the course and lecture; follow "
                     "their page links for source text. Free: no API quota.",
            {"query": {"type": "string", "description": "specific terms, e.g. 'dynamic programming subproblem memo'"},
             "k": {"type": "integer", "description": "number of excerpts (default 5, max 10)"}}, ["query"]),
]


def _corpus_ready():
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
        try:
            return con.execute("SELECT 1 FROM files WHERE nchunks > 0 LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


# ─── system prompt ───────────────────────────────────────────────────────────
def build_system_prompt():
    cwd = os.getcwd()
    git = ""
    if shutil.which("git"):
        r = subprocess.run("git rev-parse --abbrev-ref HEAD && git status --short | head -20",
                           shell=True, capture_output=True, text=True, cwd=cwd)
        if r.returncode == 0:
            git = f"\nGit branch and status:\n{r.stdout.strip()}"
    project = ""
    for name in ("FLINT.md", "AGENTS.md"):
        f = Path(cwd) / name
        if f.exists():
            project += f"\n\nProject instructions from {name}:\n{f.read_text(errors='replace')[:8000]}"
    if _corpus_ready():
        project += ("\n\nA `study` tool searches local MIT OpenCourseWare material (deep learning and machine learning, probability, matrix calculus, discrete math and combinatorics, algorithms and Python, mathematical finance, fintech, blockchain, risk and decision analysis, venture finance, microeconomics, game theory, public finance, perception and psychology) "
                    "and past code reviews. When a problem calls for a known algorithm, model or technique, "
                    "look it up there and cite the course and lecture it came from.")
    return f"""You are flint, a coding agent running in the user's terminal.
You help with software tasks by reading, searching, editing files and running commands through your tools.

How to work:
- Investigate before acting: list_files / search / read_file to understand the code first.
- Always read_file before edit_file. Keep old_str small but unique, copied exactly.
- Make the smallest change that solves the task. Don't rewrite files that only need a tweak.
- After changing code, verify it: run the tests, the build, or the script with bash.
- If a tool returns an error, read it and fix your approach instead of repeating the same call.
- Never print or exfiltrate secrets, keys, or wallet material you come across.
- If the user denies a tool call, respect it and ask or adjust.
- Replies are shown in a terminal: be brief, use markdown sparingly, no filler.
- When the task is done, say what you changed in 1-3 sentences.

Environment:
- Working directory: {cwd}
- OS: {platform.system()} {platform.release()} ({platform.machine()})
- Date: {datetime.date.today().isoformat()}{git}{project}"""


# ─── rate limits ─────────────────────────────────────────────────────────────
class DailyCapReached(Exception):
    def __init__(self, reset_at):
        super().__init__("daily free-model request cap reached")
        self.reset_at = reset_at


class OutOfCredits(Exception):
    pass


class StepLimitReached(Exception):
    pass


class IncompleteResponse(Exception):
    def __init__(self, msg, finish_reason=None):
        super().__init__(msg)
        self.finish_reason = finish_reason


class BudgetPaused(Exception):
    pass


class ProviderUnavailable(Exception):
    """The model or its providers cannot serve requests right now (overload, 404, 5xx)."""


def _next_utc_midnight():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                      microsecond=0).timestamp()


def fmt_reset(ts):
    local = datetime.datetime.fromtimestamp(ts)
    mins = max(0, int((ts - time.time()) // 60))
    return f"{local:%H:%M} local (in {mins // 60}h {mins % 60}m)"


class Throttle:
    """One request budget shared by every flint process on this machine.

    Keeps a rolling 60s window under RPM_LIMIT, counts requests per UTC day,
    and remembers a daily-cap block so a swarm stops instantly instead of each
    agent separately burning a failed request to discover it."""

    def __init__(self, rpm=RPM_LIMIT):
        if rpm < 1:
            raise ValueError("FLINT_RPM must be positive")
        self.rpm = rpm
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = STATE_DIR / "requests.json"
        self.lock = STATE_DIR / "requests.lock"

    def _txn(self, fn):
        with open(self.lock, "w") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                d = json.loads(self.path.read_text()) if self.path.exists() else {}
            except (json.JSONDecodeError, OSError):
                d = {}
            day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            if d.get("day") != day:
                d.update(day=day, count=0)
            out = fn(d)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)
            return out

    def acquire(self, on_wait=None):
        while True:
            def step(d):
                now = time.time()
                if d.get("blocked_until", 0) > now:
                    raise DailyCapReached(d["blocked_until"])
                policy = os.environ.get("FLINT_SWARM_BUDGET")
                if policy:
                    from swarm.budget import Budget
                    allowed, delay, reason = Budget(**json.loads(policy)).check(state=d)
                    if not allowed:
                        raise BudgetPaused(f"{reason}; check again in {delay:.0f}s")
                recent = [t for t in d.get("recent", []) if now - t < 60]
                d["recent"] = recent
                if len(recent) < self.rpm:
                    recent.append(now)
                    d["count"] = d.get("count", 0) + 1
                    return 0
                return 60 - (now - recent[0]) + 0.2
            wait = self._txn(step)
            if wait <= 0:
                return
            if on_wait:
                on_wait(wait)
            time.sleep(wait)

    def block(self, until):
        self._txn(lambda d: d.update(blocked_until=until))

    def clear_block(self):
        self._txn(lambda d: d.pop("blocked_until", None))

    def today(self):
        return self._txn(lambda d: d.get("count", 0))


def classify_429(e):
    """Return (kind, reset_at, message). kind: daily | minute | provider | unknown."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    err = err if isinstance(err, dict) else {}
    msg = str(err.get("message") or getattr(e, "message", "") or e)
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    headers = {k.lower(): v for k, v in (meta.get("headers") or {}).items()}
    resp = getattr(e, "response", None)
    if resp is not None:
        headers.update({k.lower(): v for k, v in resp.headers.items()})
    reset_at = None
    try:
        r = float(headers.get("x-ratelimit-reset", ""))
        reset_at = r / 1000 if r > 1e11 else r  # header is usually epoch milliseconds
    except ValueError:
        pass
    low = msg.lower()
    if any(s in low for s in ("per-day", "per day", "daily", "free-models-per-day")):
        kind = "daily"
    elif any(s in low for s in ("per-min", "per minute", "free-models-per-min")):
        kind = "minute"
    elif meta.get("provider_name") or "upstream" in low or "provider" in low:
        kind = "provider"
    elif reset_at and reset_at - time.time() > 600:
        kind = "daily"
    else:
        kind = "unknown"
    return kind, reset_at, msg


def _ssl_context():
    """urllib on python.org macOS builds has no CA bundle; use the system trust store
    the same way the openai client does."""
    try:
        import ssl
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def openrouter_get(path, api_key, timeout=10):
    req = urllib.request.Request(OPENROUTER + path, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return json.loads(r.read())


def fetch_key_info(api_key):
    """Account info from OpenRouter: is_free_tier, usage, limit_remaining, ..."""
    for path in ("/key", "/auth/key"):
        try:
            return openrouter_get(path, api_key).get("data", {})
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


# ─── agent ───────────────────────────────────────────────────────────────────
class Agent:
    def __init__(self, model, yolo=False, headless=False, read_only=False):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            print("OPENROUTER_API_KEY is not set. Add it to .env beside flint.py "
                  "or export it in your shell.", file=sys.stderr)
            sys.exit(1)
        self.api_key = key
        self.client = OpenAI(base_url=OPENROUTER, api_key=key, max_retries=0,
                             timeout=float(os.environ.get("FLINT_TIMEOUT", "90")),
                             default_headers={"X-Title": "flint"})
        self.throttle = Throttle()
        self.model = model
        self.yolo = yolo
        self.headless = headless
        self.read_only = read_only or (headless and not yolo)
        self.corpus = _corpus_ready()
        if headless:
            os.environ["FLINT_HEADLESS"] = "1"
        self.always = set()
        self.last_reasoning = ""
        self.last_prompt_tokens = 0
        self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": build_system_prompt()}]
        if self.read_only:
            self.messages[0]["content"] += "\nRead-only mode: inspect and advise; you cannot edit files or run shell commands."

    # ── model call ──
    def _request_kwargs(self):
        schemas = [t for t in TOOL_SCHEMAS
                   if (not self.read_only or t["function"]["name"] not in NEEDS_APPROVAL)
                   and (t["function"]["name"] != "study" or getattr(self, "corpus", False))]
        kw = dict(model=self.model, messages=self.messages, tools=schemas,
                  stream=True, stream_options={"include_usage": True})
        if getattr(self, "final_answer", False):
            kw["tool_choice"] = "none"
        if FALLBACKS:
            kw["extra_body"] = {"models": [self.model] + [m for m in FALLBACKS if m != self.model]}
        return kw

    def _note(self, msg):
        (console.print if not self.headless else
         (lambda m: print(re.sub(r"\[/?[^\]]*\]", "", m), file=sys.stderr)))(msg)

    def complete(self):
        # Failed attempts can still count toward the daily quota, so retries are few and deliberate.
        provider_retries = 0
        for attempt in range(8):
            self.throttle.acquire(on_wait=lambda s: self._note(
                f"[dim]pacing: {RPM_LIMIT}/min budget used, waiting {s:.0f}s[/]"))
            try:
                return self._stream_once()
            except IncompleteResponse as e:
                # finish_reason "error" means the upstream provider failed mid-reply; the model
                # did not answer badly. Retry briefly, then report the provider as unavailable
                # (exit 7), which rests the model instead of scoring it.
                if e.finish_reason != "error":
                    raise
                provider_retries += 1
                if provider_retries > 2:
                    raise ProviderUnavailable(f"{self.model}: provider kept failing mid-reply: {e}") from e
                wait = 5 * 2 ** (provider_retries - 1)
                self._note(f"[yellow]provider failed mid-reply — retry {provider_retries}/2 in {wait}s[/]")
                time.sleep(wait)
                continue
            except APIError as e:
                body = e.body if isinstance(e.body, dict) else {}
                body = body.get("error", body)
                code = getattr(e, "status_code", None) or (body.get("code") if isinstance(body, dict) else None)
                if isinstance(e, APIConnectionError) or str(code) in ("500", "502", "503", "504", "529"):
                    if attempt >= 3:
                        raise ProviderUnavailable(f"{self.model}: repeated connection/provider errors: {e}") from e
                    wait = 3 * 2 ** attempt
                    self._note(f"[yellow]connection/provider error — retry {attempt + 1}/3 in {wait}s[/]")
                    time.sleep(wait)
                    continue
                if str(code) == "402":
                    raise OutOfCredits("OpenRouter returned 402: insufficient credits or account limit reached.") from e
                if str(code) == "404":
                    raise ProviderUnavailable(f"{self.model}: model or endpoint not available (404): {e}") from e
                if not isinstance(e, RateLimitError) and str(code) != "429":
                    raise
                kind, reset_at, msg = classify_429(e)
                if kind == "daily":
                    reset_at = reset_at or _next_utc_midnight()
                    self.throttle.block(reset_at)
                    raise DailyCapReached(reset_at)
                if kind == "minute":
                    wait = max(5, min(65, (reset_at or time.time() + 30) - time.time()))
                    self._note(f"[yellow]per-minute cap hit — waiting {wait:.0f}s[/]")
                    time.sleep(wait)
                    continue
                provider_retries += 1
                if provider_retries > 3:
                    raise ProviderUnavailable(f"model provider is overloaded: {msg}. Try again later "
                                              "or set FLINT_FALLBACKS to a backup model.")
                wait = 10 * 2 ** (provider_retries - 1)
                self._note(f"[yellow]provider busy ({kind}) — retry {provider_retries}/3 in {wait}s: {msg[:250]}[/]")
                time.sleep(wait)
        raise ProviderUnavailable("Gave up after repeated errors.")

    def _stream_once(self):
        stream = self.client.chat.completions.create(**self._request_kwargs())
        content, reasoning, calls = "", "", {}
        finish_reason = None
        live = None
        status = None if self.headless else console.status("[dim]thinking…[/]", spinner="dots")
        if status:
            status.start()
        try:
            for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage and getattr(usage, "prompt_tokens", None):
                    self.last_prompt_tokens = usage.prompt_tokens
                if not chunk.choices:
                    continue
                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta
                r = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                if r:
                    reasoning += r
                    if status:
                        status.update(f"[dim]thinking… ({len(reasoning) // 4} tokens)[/]")
                if delta.content:
                    content += delta.content
                    if not self.headless:
                        if live is None:
                            if status:
                                status.stop()
                            live = Live(Markdown(content), console=console,
                                        refresh_per_second=12, vertical_overflow="visible")
                            live.start()
                        live.update(Markdown(content))
                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else (
                        max(calls) if calls and not tc.id else len(calls))
                    slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = tc.function
                    if fn and fn.name and not slot["name"]:
                        slot["name"] = fn.name
                    if fn and fn.arguments:
                        slot["args"] += fn.arguments
        finally:
            stream.close()
            if status:
                status.stop()
            if live:
                live.stop()
        self.last_reasoning = reasoning
        if finish_reason not in ("stop", "tool_calls"):
            raise IncompleteResponse(f"Model response incomplete (finish_reason={finish_reason!r}); no tools executed.",
                                     finish_reason)
        if not content.strip() and not calls:
            raise IncompleteResponse("Model returned an empty response.")
        return content, [calls[k] for k in sorted(calls)]

    # ── tool execution ──
    def _preview(self, name, args):
        if name == "bash":
            return Syntax(args.get("command", ""), "bash", word_wrap=True)
        if name == "write_file":
            lexer = Syntax.guess_lexer(args.get("path", ""), args.get("content", ""))
            body = "\n".join(args.get("content", "").splitlines()[:40])
            return Syntax(body, lexer, line_numbers=True, word_wrap=True)
        if name == "edit_file":
            old, new, err = _plan_edit(args.get("path", ""), args.get("old_str", ""),
                                       args.get("new_str", ""), args.get("replace_all", False))
            if err:
                return None
            diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                "before", "after", n=2))
            return Syntax(diff or "(no change)", "diff", word_wrap=True)
        return None

    def _approve(self, name, args):
        if self.yolo or name in self.always:
            return True, None
        if self.headless:
            return False, "Denied: running non-interactively without --yolo."
        preview = self._preview(name, args)
        if preview is None and name == "edit_file":
            return True, None  # edit will fail with a helpful error; no need to ask
        console.print(Panel(preview, border_style=ACCENT, title=name, title_align="left"))
        ans = console.input(f"[bold]allow?[/] [green]y[/] yes · [red]n[/] no · "
                            f"[{ACCENT}]a[/] always ({name}) · or type instructions › ").strip()
        low = ans.lower()
        if low in ("y", "yes"):
            return True, None
        if low in ("a", "always"):
            self.always.add(name)
            return True, None
        if low in ("", "n", "no"):
            return False, "The user denied this tool call."
        return False, f"The user denied this tool call and said: {ans}"

    @staticmethod
    def _summary(name, args):
        key = {"bash": "command", "search": "pattern", "list_files": "pattern"}.get(name, "path")
        s = str(args.get(key, "")).replace("\n", " ")
        return s if len(s) < 80 else s[:77] + "…"

    def run_tool(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: tool arguments were not valid JSON ({e}). Retry with a valid JSON object."
        fn = TOOLS.get(name)
        if not fn:
            return f"Error: unknown tool '{name}'. Available: {', '.join(TOOLS)}."
        if self.read_only and name in NEEDS_APPROVAL:
            return "Denied: read-only mode does not allow edits or shell commands."

        if not self.headless:
            console.print(Text.assemble(("◆ ", ACCENT), (name, "bold"),
                                        (f"({self._summary(name, args)})", "dim")))
        else:
            print(f"tool: {name}", file=sys.stderr, flush=True)
        if name in NEEDS_APPROVAL:
            ok, reason = self._approve(name, args)
            if not ok:
                if not self.headless:
                    console.print("  [red]└─ denied[/]")
                return reason
        try:
            out = fn(**args)
        except TypeError as e:
            out = f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # tools should never crash the agent
            out = f"Error: {type(e).__name__}: {e}"

        if not self.headless:
            lines = out.splitlines() or [""]
            shown = "\n     ".join(l[:160] for l in lines[:4])
            more = f"\n     … +{len(lines) - 4} lines" if len(lines) > 4 else ""
            style = "red" if out.startswith("Error") else "dim"
            console.print(Text(f"  └─ {shown}{more}", style=style))
        return _truncate(out)

    # ── turn loop ──
    def save_checkpoint(self):
        path = getattr(self, "checkpoint_path", None)
        if path:
            from nonstop import save_checkpoint
            save_checkpoint(path, self.messages)

    def turn(self, user_text):
        self.messages.append({"role": "user", "content": user_text})
        content = ""
        for step in range(MAX_STEPS):
            if self.headless:
                print(f"round {step + 1}/{MAX_STEPS}: requesting {self.model}", file=sys.stderr, flush=True)
            content, calls = self.complete()
            msg = {"role": "assistant", "content": content or ""}
            if calls:
                msg["tool_calls"] = [{
                    "id": c["id"] or f"call_{len(self.messages)}_{i}", "type": "function",
                    "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in enumerate(calls)]
            self.messages.append(msg)
            self.save_checkpoint()
            if not calls:
                break
            for c in msg["tool_calls"]:
                result = self.run_tool(c)
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                self.save_checkpoint()
        else:
            content = self._final_answer()
        if self.last_prompt_tokens > CONTEXT_WARN_TOKENS:
            self._note("[yellow]context is getting large — consider /clear[/]")
        return content

    FINAL_NUDGE = ("You have used every tool round this turn allows. Do not call any tools. "
                   "Give your final answer now, in exactly the format the task asked for.")

    def _final_answer(self):
        """After the step limit, a headless turn gets one tool-free round to answer, so the
        rounds already spent are not wasted (a reviewer that read everything but never wrote
        its verdict). Interactive turns stop as before."""
        limit = StepLimitReached(f"Stopped after {MAX_STEPS} rounds; task is incomplete. Changes may already exist.")
        if not self.headless:
            raise limit
        print(f"step limit: asking {self.model} for a final answer without tools", file=sys.stderr, flush=True)
        self.messages.append({"role": "user", "content": self.FINAL_NUDGE})
        self.final_answer = True
        try:
            content, calls = self.complete()
        except (DailyCapReached, OutOfCredits, BudgetPaused, ProviderUnavailable, KeyboardInterrupt):
            raise
        except Exception as e:
            raise limit from e
        finally:
            self.final_answer = False
        if calls or not content.strip():   # still reaching for tools: no answer, and none run
            raise limit
        self.messages.append({"role": "assistant", "content": content})
        self.save_checkpoint()
        return content

    def repair_after_interrupt(self):
        """Make history valid again if Ctrl+C landed mid tool-round."""
        answered = {m.get("tool_call_id") for m in self.messages if m["role"] == "tool"}
        for m in reversed(self.messages):
            if m["role"] == "assistant" and m.get("tool_calls"):
                for c in m["tool_calls"]:
                    if c["id"] not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": c["id"],
                                              "content": "Interrupted by the user."})
                break
        if self.messages[-1]["role"] == "user":
            self.messages.pop()


# ─── ui ──────────────────────────────────────────────────────────────────────
MASCOT = [
    " ▗▄▄▄▄▖ ",
    " ▐▘◆◆▝▌ ",
    " ▐ ▔▔ ▌ ",
    "  ▀▘▝▀  ",
]

HELP = """[bold]commands[/]
  /help            this list
  /clear           start a fresh conversation
  /model [slug]    show or switch the OpenRouter model
  /yolo            toggle auto-approve for edits and shell commands
  /nonstop [for Nh]: goal   unattended edits/tests (default 8 hours)
  /think           show the model's reasoning from the last reply
  /tokens          context size of the last request
  /usage           account tier, credits, and requests used today
  /study <query>   search the local study corpus (no API quota)
  /exit            quit (or Ctrl+D)
[dim]Ctrl+C interrupts the current turn. Put project notes in FLINT.md or AGENTS.md.[/]"""


def banner(agent):
    cwd = os.getcwd().replace(str(Path.home()), "~")
    side = ["[bold]flint[/]", f"[dim]{agent.model}[/]", f"[dim]{cwd}[/]",
            "[dim]/help for commands[/]"]
    rows = [f"[{ACCENT}]{m}[/]  {s}" for m, s in zip(MASCOT, side)]
    console.print(Panel("\n".join(rows), border_style=ACCENT, expand=False))


def show_usage(agent):
    info = fetch_key_info(agent.api_key)
    today = agent.throttle.today()
    if info is None:
        console.print(f"[dim]couldn't reach OpenRouter for account info.[/] requests today (local count): {today}")
        return
    quota = info.get("free_model_daily_requests") or {}
    tier = ("[yellow]free tier — no credits purchased yet[/]" if info.get("is_free_tier")
            else "[green]credits purchased[/]")
    remaining = info.get("limit_remaining")
    console.print(f"[dim]account:[/] {tier}\n"
                  f"[dim]requests today (local attempts):[/] {today}\n"
                  f"[dim]free-model requests (provider):[/] {quota.get('used', 'unknown')} / {quota.get('limit', 'unknown')}\n"
                  f"[dim]paid usage today:[/] ${info.get('usage_daily', 0):.4f}"
                  + (f"   [dim]key limit remaining:[/] ${remaining:.2f}" if remaining is not None else ""))


def handle_command(agent, line):
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    if cmd in ("/exit", "/quit"):
        raise EOFError
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/clear":
        agent.reset()
        console.print("[dim]conversation cleared[/]")
    elif cmd == "/model":
        if arg:
            agent.model = arg
        console.print(f"[dim]model:[/] {agent.model}")
    elif cmd == "/yolo":
        agent.yolo = not agent.yolo
        console.print(f"[dim]auto-approve:[/] {'[red]on[/]' if agent.yolo else 'off'}")
    elif cmd == "/think":
        console.print(Text(agent.last_reasoning or "(no reasoning captured)", style="dim italic"))
    elif cmd == "/usage":
        show_usage(agent)
    elif cmd == "/study":
        console.print(Text(study(arg) if arg else "usage: /study <query>", style="dim"))
    elif cmd == "/tokens":
        console.print(f"[dim]last prompt:[/] {agent.last_prompt_tokens:,} tokens")
    else:
        console.print(f"[red]unknown command {cmd}[/] — /help")


def repl(agent):
    banner(agent)
    while True:
        try:
            console.print()
            line = console.input(f"[bold {ACCENT}]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/]")
            return
        if not line:
            continue
        from nonstop import parse_trigger, resolve_goal
        trigger = parse_trigger(line)
        if trigger is not None:
            if agent.read_only:
                console.print("[red]Nonstop mode needs edits and shell access; restart without --read-only.[/]")
                continue
            hours, goal = trigger
            previous = next((m["content"] for m in reversed(agent.messages) if m["role"] == "user"), "")
            try:
                goal = resolve_goal(goal, os.getcwd(), previous)
                run_unattended(goal, agent.model, hours if hours is not None else 8)
                agent.reset()  # Files may have changed substantially during the run.
            except ValueError as e:
                console.print(f"[red]{e}[/]")
            continue
        if line.startswith("/"):
            try:
                handle_command(agent, line)
            except EOFError:
                console.print("[dim]bye[/]")
                return
            continue
        try:
            agent.turn(line)
            console.print(f"[dim]· {agent.throttle.today()} requests today[/]")
        except KeyboardInterrupt:
            agent.repair_after_interrupt()
            console.print("\n[yellow]interrupted[/]")
        except DailyCapReached as e:
            agent.repair_after_interrupt()
            console.print(f"[red]daily free-model cap reached.[/] Resets {fmt_reset(e.reset_at)}. "
                          "Switch to a paid model with /model to keep going.")
        except OutOfCredits as e:
            agent.repair_after_interrupt()
            console.print(f"[red]{e}[/]")
        except Exception as e:
            agent.repair_after_interrupt()
            console.print(f"[red]error:[/] {e}")


def run_unattended(goal, model, hours=8, test_command=None):
    from nonstop import run_nonstop
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("OPENROUTER_API_KEY is not set; add it to .env beside flint.py.")
    return run_nonstop(goal, os.getcwd(), model, STATE_DIR, hours, test_command)


def main():
    ap = argparse.ArgumentParser(description="flint — a tiny terminal coding agent on OpenRouter")
    ap.add_argument("-p", "--prompt", help="run one task non-interactively and print the result")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"model slug (default {DEFAULT_MODEL})")
    ap.add_argument("--yolo", action="store_true", help="auto-approve edits and shell commands")
    ap.add_argument("--read-only", action="store_true", help="allow only file reading, listing and searching")
    ap.add_argument("--nonstop", action="store_true", help="unattended work cycles; enables edits and shell commands")
    ap.add_argument("--hours", type=float, help="nonstop duration (default 8 hours)")
    ap.add_argument("--test-command", help="shell verification command to run after each nonstop work cycle")
    ap.add_argument("--checkpoint", help=argparse.SUPPRESS)
    ap.add_argument("-C", "--cwd", help="project directory to work in")
    ap.add_argument("--reset-cap", action="store_true",
                    help="clear a remembered daily-cap block (e.g. after buying credits)")
    a = ap.parse_args()
    if a.cwd:
        os.chdir(Path(a.cwd).expanduser())
    if a.reset_cap:
        Throttle().clear_block()

    prompt = (a.prompt if a.prompt != "-" else sys.stdin.read()) if a.prompt is not None else ""
    from nonstop import parse_trigger
    trigger = parse_trigger(prompt)
    if a.nonstop or trigger is not None:
        if a.read_only:
            ap.error("nonstop mode enables edits and shell commands; it cannot use --read-only")
        phrase_hours, goal = trigger if trigger is not None else (None, prompt)
        hours = a.hours if a.hours is not None else (phrase_hours if phrase_hours is not None else 8)
        try:
            sys.exit(run_unattended(goal, a.model, hours, a.test_command))
        except ValueError as e:
            ap.error(str(e))
    if a.hours is not None or a.test_command:
        ap.error("--hours and --test-command require --nonstop or a 'run this nonstop' prompt")

    if a.prompt is not None:
        agent = Agent(a.model, yolo=a.yolo, headless=True, read_only=a.read_only)
        agent.checkpoint_path = a.checkpoint
        if agent.read_only:
            print("flint: read-only mode; use --yolo to allow edits and shell commands.", file=sys.stderr)
        try:
            print(agent.turn(prompt))
        except KeyboardInterrupt:
            print("\nflint: interrupted; changes already made remain on disk.", file=sys.stderr)
            sys.exit(130)
        except StepLimitReached as e:
            print(f"flint: {e}", file=sys.stderr)
            sys.exit(5)
        except BudgetPaused as e:
            print(f"flint: budget paused: {e}", file=sys.stderr)
            sys.exit(6)
        except DailyCapReached as e:
            print(f"flint: daily free-model cap reached, resets {fmt_reset(e.reset_at)}", file=sys.stderr)
            sys.exit(3)  # distinct code so a swarm orchestrator can back off
        except OutOfCredits as e:
            print(f"flint: {e}", file=sys.stderr)
            sys.exit(4)
        except ProviderUnavailable as e:
            print(f"flint: provider unavailable: {e}", file=sys.stderr)
            sys.exit(7)  # the model is down, not wrong: the swarm cools it down
        except Exception as e:
            print(f"flint: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            agent.save_checkpoint()
    else:
        repl(Agent(a.model, yolo=a.yolo, read_only=a.read_only))


if __name__ == "__main__":
    main()
