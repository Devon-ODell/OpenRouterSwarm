#!/usr/bin/env python3
"""JSON bridge between an editor (the Flint Swarm extension for Cursor/VS Code) and the swarm.

Every command prints JSON. `ask` streams one JSON object per line as models finish.

    bridge.py info
    bridge.py status   --repo PATH
    bridge.py ask      --repo PATH --question Q [--file F --start N --end M --selection-file S]
                       [--models 3 | --model a:free,b:free] [--no-synthesis] [--no-study]
                       [--paid off|auto|always]
    bridge.py task     --repo PATH --title T [--detail D] [--file F --start N --end M --selection-file S]
    bridge.py queue-get    --repo PATH --id ID
    bridge.py queue-edit   --repo PATH --id ID [--title T] [--detail D] [--kind K]
                           [--priority N] [--acceptance TEXT ...]
                           [--allow-test-changes | --no-allow-test-changes]
    bridge.py queue-retry   --repo PATH --id ID
    bridge.py queue-requeue --repo PATH --id ID
    bridge.py queue-remove --repo PATH --id ID [--cascade]
    bridge.py landed   --repo PATH [--limit 20]
    bridge.py import-bugs --repo PATH [--limit 5] [--file PATH]
    bridge.py parked   --repo PATH [--limit 20]
    bridge.py queue-clear  --repo PATH [--include-claimed]
    bridge.py study    --query Q [-k 6]
    bridge.py report   --repo PATH [--hours 24]
    bridge.py vote     --model M --useful 1|0
    bridge.py wallet   [--cap 5] [--reset] [--enable | --disable] [--account]
    bridge.py local-models   [--probe]
    bridge.py grind-cmd --repo PATH [--goal G] [--test-cmd T] [--hours H]
    bridge.py stop     --repo PATH [--drain] | --all

`local-models` lists every local server's pulled models (GET /models on each local base
URL) for the editor's provider dropdown; `ask` accepts --provider (ollama, lm-studio, mlx,
llamacpp, openai, anthropic, openrouter) and --base-url for a custom endpoint, and a local
ask never touches the OpenRouter key, wallet or request budget.

`ask` runs read-only flint agents (read_file, list_files, search, study; no edits, no shell)
in the macOS sandbox, several free models in parallel, then one more model merges their
answers. Answers the user marks useful reinforce that model for later questions (Thompson
sampling, role "consult"). Requests count against the same daily allowance as the swarm but
not against the swarm's reserve, which exists for exactly this kind of use.

Free models are the default and the swarm itself can use nothing else. The editor is the one
exception: `editor_wallet` in swarm/config.json is a fixed pot of real credits (default $5) that
`ask` may spend on a paid model when no free model answered, so a question is not simply lost to
rate limits. Every charge is the number OpenRouter reports, recorded in ~/.flint/wallet.json, and
the pot is checked before each request. `bridge.py wallet` reads and sets it.
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)   # the sandbox cannot read .env; children get the key via env

import sandbox  # noqa: E402
import swarmd  # noqa: E402
import wallet  # noqa: E402
from learn import Ledger  # noqa: E402
# swarm/ is on sys.path (inserted above), so providers resolves as a plain module here;
# flint.py imports it as `from swarm import providers` once the repo root is on its path.
import providers  # noqa: E402

CONSULT = HERE / "state" / "consult" / "learn.json"
MAX_SELECTION = 12_000
MAX_PAID_PER_ASK = 2          # a rescue is meant to cost cents, not the pot
# Tried in this order, but only after checking they still exist and still take tools: a retired
# slug exits with "model not available", which is the same dead end as no answer at all. When
# none of them survives, the catalog's cheap tier is used instead.
PAID_RESCUE = ("anthropic/claude-haiku-4.5", "google/gemini-2.5-flash", "openai/gpt-5-mini",
               "deepseek/deepseek-chat", "qwen/qwen3-coder")
CHEAP_TIER = re.compile(r"(mini|flash|haiku|small|lite|nano|coder|turbo)")
_children, _children_lock, _out_lock = set(), threading.Lock(), threading.Lock()


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _kill_children(*_):
    with _children_lock:
        for p in list(_children):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    os._exit(143)


def config_for(repo=None):
    """The swarm config for `repo` when given, else the default one. Only the configured repo is
    ground by a running daemon; other repos still get their own queue and state.

    A repository with a tuned file in swarm/configs/ gets that file's settings — its test
    command, its pool, its cooldowns — so the panel and the daemon agree about what is running."""
    c = swarmd.load_cfg(repo)
    c["python"] = swarmd.python_for(c)
    if repo:
        repo = str(Path(repo).expanduser().resolve())
        if c.get("repo") != repo:
            branch = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
            c = dict(c, repo=repo, base_branch=branch if branch and branch != "HEAD" else "main")
    return c


def is_target(repo):
    configured = swarmd.load_cfg().get("repo", "")
    try:
        return Path(configured).expanduser().resolve() == Path(repo).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def daemon_running():
    """True when a swarm daemon holds this repo's lock (swarmd.use_repo must have run)."""
    lock = swarmd.STATE / "daemon.lock"
    if not lock.exists():
        return False
    import fcntl
    with open(lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def wallet_for(c):
    """The editor's dollar allowance, or None when it is switched off or empty. Only the bridge
    ever builds one, so only editor-initiated turns can reach the account's credits; the grind
    daemon keeps running on free models."""
    w = c.get("editor_wallet") or {}
    if w.get("enabled") is False:
        return None
    try:
        cap = float(os.environ.get("FLINT_EDITOR_CAP") or w.get("cap_usd") or wallet.DEFAULT_CAP)
    except (TypeError, ValueError):
        cap = wallet.DEFAULT_CAP
    return wallet.Wallet(cap=cap, label="cursor") if cap > 0 else None


def _catalog():
    """OpenRouter's model list, cached for an hour in FLINT_HOME."""
    cache = Path(os.environ.get("FLINT_HOME", "~/.flint")).expanduser() / "models.json"
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < 3600:
            return json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    import flint as F
    data = F.openrouter_get("/models", os.environ.get("OPENROUTER_API_KEY", ""), timeout=20)["data"]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def _token_price(m):
    """Rough cost of one turn's worth of tokens, weighting the prompt: agents read a lot."""
    p = m.get("pricing") or {}
    try:
        return float(p.get("prompt") or 0) * 3 + float(p.get("completion") or 0)
    except (TypeError, ValueError):
        return float("inf")


def paid_models(c, n=1):
    """Paid models the wallet may spend on: the configured ones that are still real, then the
    catalog's cheap tier, cheapest first. Tool support is required — an agent without tools
    cannot read the code it is being asked about."""
    wanted = list((c.get("editor_wallet") or {}).get("paid_models") or PAID_RESCUE)
    try:
        catalog = {m["id"]: m for m in _catalog()}
    except Exception:
        return wanted[:n]                     # catalog unreachable: trust the configuration

    def usable(m):
        return (not m["id"].endswith(":free") and _token_price(m) < float("inf")
                and "tools" in (m.get("supported_parameters") or []))

    picked = [s for s in wanted if s in catalog and usable(catalog[s])]
    rest = sorted((m for m in catalog.values() if usable(m) and m["id"] not in picked),
                  key=_token_price)
    cheap = [m["id"] for m in rest if CHEAP_TIER.search(m["id"])] or [m["id"] for m in rest]
    return (picked + cheap)[:n]


def selection_context(repo, file=None, start=None, end=None, selection_file=None):
    """(label, code) for the code the user pointed at: their selection, else the file's lines."""
    if not file:
        return None, None
    path = Path(file).expanduser()
    try:
        rel = str(path.resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        rel = str(path)
    code = ""
    if selection_file:
        code = Path(selection_file).read_text(errors="replace")
    elif path.is_file():
        lines = path.read_text(errors="replace").splitlines()
        a = max(1, int(start or 1))
        b = min(len(lines), int(end or len(lines)))
        code = "\n".join(lines[a - 1:b])
    if len(code) > MAX_SELECTION:
        code = code[:MAX_SELECTION] + f"\n… [{len(code) - MAX_SELECTION} more characters not shown; read the file]"
    where = rel + (f" lines {start}-{end}" if start and end else "")
    return where, code


# ------------------------------------------------------------------ ask

ASK = """You are one of several independent models a developer asked about their code in Cursor.
Answer on your own merits; the answers will be compared.

QUESTION
{question}
{context}
You are in the repository root and can read files (read_file, list_files, search) to check
surrounding code before answering; do it rather than guessing.{study}

Be concrete: cite file:line for claims about the code, show corrected code when you find a bug,
and when a technique comes from the MIT material, name the course and lecture. If you are unsure,
say what would settle it. Keep the answer under 300 words unless code is needed."""

STUDY_HINT = ("\nA `study` tool searches MIT OpenCourseWare lecture cards and source pages "
              "(algorithms, Python, machine learning, probability, finance, economics...). Use it "
              "when a known algorithm, data structure or model applies, then read_file the source "
              "page it cites before relying on it.")

SYNTH = """Several models answered a developer's question about their code. Write the single best
answer for the developer.

QUESTION
{question}
{context}
ANSWERS
{answers}

Check the claims that matter against the code with read_file before keeping them. Keep what is
correct and useful, drop what is wrong (say briefly why), and point out real disagreements.
Credit the model behind each key point in brackets, e.g. [qwen/qwen3.8-27b:free]. Under 300 words
unless code is needed."""


def run_flint(prompt, repo, model, steps, study=True, timeout=420, on_progress=None, purse=None,
              provider=None):
    """One read-only flint turn. on_progress receives its "round …"/"tool: …" lines as they happen.

    `purse` is a Wallet, passed only for a paid model: a free turn must never be stopped because
    the dollar allowance is empty. `provider` names the backend (ollama, lm-studio, …) when the
    editor picked one; flint's own registry resolves the endpoint and skips the OpenRouter
    throttle/wallet for local backends.
    """
    env = dict(os.environ, FLINT_MAX_STEPS=str(steps))
    env.pop("FLINT_SWARM_BUDGET", None)
    env.pop("FLINT_WALLET", None)
    if purse is not None:
        env["FLINT_WALLET"] = purse.env(label="cursor ask")
    if not study:
        env["FLINT_CORPUS_DB"] = swarmd.NO_CORPUS
    cmd = [sys.executable, str(ROOT / "flint.py"), "-p", prompt, "-C", str(repo), "-m", model, "--read-only"]
    if provider and provider != "openrouter":
        cmd += ["--provider", provider]
    if sandbox.available():
        home = os.environ.get("FLINT_HOME", "~/.flint")
        cmd = sandbox.wrap(cmd, sandbox.profile([home]))
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                         start_new_session=True)
    with _children_lock:
        _children.add(p)
    lines, expired = [], threading.Event()

    def pump():
        for line in p.stderr:
            lines.append(line)
            s = line.strip()
            if on_progress and s.startswith(("round ", "tool: ", "step limit", "provider",
                                             "per-minute", "pacing", "spend: ", "cost: ")):
                on_progress(s)

    def expire():
        expired.set()
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    reader, timer = threading.Thread(target=pump, daemon=True), threading.Timer(timeout, expire)
    reader.start()
    timer.start()
    try:
        out = p.stdout.read()
        p.wait()
    finally:
        timer.cancel()
        reader.join(timeout=5)
        with _children_lock:
            _children.discard(p)
    err = "".join(lines)
    if expired.is_set():
        rounds = len(re.findall(r"^round \d+/", err, re.M))
        return {"ok": False, "model": model, "error": f"timed out after {timeout}s ({rounds} requests made)",
                "secs": round(time.time() - t0), "requests": rounds, "exit": None,
                "paid": purse is not None, "usd": None}
    studied = len(re.findall(r"^tool: study$", err or "", re.M))
    tools = len(re.findall(r"^tool: ", err or "", re.M))
    rounds = len(re.findall(r"^round \d+/", err or "", re.M))
    spent = re.search(r"^cost: \$([0-9.]+) this turn", err or "", re.M)
    base = {"model": model, "secs": round(time.time() - t0), "study_calls": studied,
            "tool_calls": tools, "requests": rounds, "exit": p.returncode,
            "paid": purse is not None, "usd": float(spent.group(1)) if spent else None}
    if p.returncode == 0 and out.strip():
        return {"ok": True, "text": out.strip(), **base}
    reason = {3: "daily free-request cap reached", 4: "credit or account limit", 5: "ran out of steps",
              6: "budget pause", 7: "provider unavailable",
              8: "model busy (rate-limited upstream)",
              9: "model not available to this API key",
              10: "paid allowance exhausted"}.get(p.returncode, "failed")
    tail = "\n".join((err or "").strip().splitlines()[-3:])
    return {"ok": False, "error": f"{reason}: {tail[-400:]}", **base}


def pick_models(c, n, explicit=None):
    pool = swarmd.pool(c)
    if explicit:
        return [m for m in explicit if m in pool or c.get("allow_paid") or m.endswith(":free")][:8]
    ledger, chosen = Ledger(CONSULT), []
    CONSULT.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(min(n, len(pool))):
        m = ledger.pick("consult", pool, exclude=set(chosen))
        if m is None:
            break
        chosen.append(m)
    return chosen


def cmd_ask(a):
    repo = str(Path(a.repo).expanduser().resolve())
    c = config_for(repo)
    provider = getattr(a, "provider", None)
    base_url = getattr(a, "base_url", None)
    local = bool(provider and provider != "openrouter" and providers.PROVIDERS.get(provider, {}).get("local")) or \
            bool(base_url)
    if not local and not os.environ.get("OPENROUTER_API_KEY"):
        emit({"event": "error", "error": f"OPENROUTER_API_KEY is not set; add it to {ROOT / '.env'}"})
        return 2
    if base_url:
        # A custom endpoint the developer typed once: persist it and point the provider at it.
        d = providers.load_providers()
        label = provider or "custom"
        d["openAIBaseUrl"] = providers.normalize_url(base_url)
        eps = [e for e in d.get("customEndpoints") or []
               if (e.get("baseUrl") or "").rstrip("/") != (base_url or "").rstrip("/")]
        eps.append({"label": label, "baseUrl": providers.normalize_url(base_url)})
        d["customEndpoints"] = eps[-8:]
        providers.save_providers(d)
        os.environ["OLLAMA_BASE_URL"] = providers.normalize_url(base_url) if provider == "ollama" else \
            os.environ.get(providers.PROVIDERS.get(provider or "", {}).get("env", ""), providers.normalize_url(base_url))
    if local and not a.model:
        # The editor picked a provider but no specific model: ask that provider's own (first)
        # or ask flint for its default for the provider.
        can = _local_models(providers.base_url_for(provider), timeout=3)
        if can:
            a.model = can[0]["id"]
        else:
            emit({"event": "error", "error": f"{provider} is not running or has no models; "
                                                f"start it (e.g. `{providers.PROVIDERS.get(provider, {}).get('command', '')}`) and try again."})
            return 2
    where, code = selection_context(repo, a.file, a.start, a.end, a.selection_file)
    context = ""
    if where:
        lang = Path(a.file).suffix.lstrip(".")
        context = f"\nCODE THE DEVELOPER POINTED AT: {where}\n```{lang}\n{code}\n```\n"
    corpus_ok = not a.no_study and Path(c.get("corpus_db") or "~/.flint/corpus.db").expanduser().is_file()
    purse = None if a.paid == "off" else wallet_for(c)
    models, paid_first = [], False
    if local:
        # The editor picked a local provider: one model, no wallet, no OpenRouter pool. If a
        # model was given use it as-is; otherwise keep the first one the server listed (set
        # above) or clear the pool so the loop below reports the server is quiet.
        models = [a.model] if a.model else []
        paid_first = False
        purse = None
    else:
        if a.paid == "always" and purse and purse.check()[0]:
            models, paid_first = paid_models(c, max(1, min(a.models, MAX_PAID_PER_ASK))), True
        if not models:
            models, paid_first = pick_models(c, a.models, a.model.split(",") if a.model else None), False
    if not models:
        emit({"event": "error", "error": f"no usable models: the {provider or 'default'} provider is "
                                            f"not running or the pool is empty"})
        return 2
    emit({"event": "context", "repo": repo, "where": where, "models": models, "study": corpus_ok,
          "paid": paid_first, "wallet": purse.snapshot() if purse else None, "provider": provider})
    prompt = ASK.format(question=a.question.strip(), context=context, study=STUDY_HINT if corpus_ok else "")
    ledger, results = Ledger(CONSULT), []

    def run_round(pool, with_purse=None):
        """Ask several models at once. Each thread reports its own result, whatever happens."""
        def one(model):
            emit({"event": "start", "model": model, "paid": with_purse is not None,
                  "provider": provider})
            try:
                kw = dict(purse=None if local else with_purse)
                if provider:
                    kw["provider"] = provider
                r = run_flint(prompt, repo, model, a.steps, corpus_ok, a.timeout,
                              lambda s: emit({"event": "progress", "model": model, "text": s}),
                              **kw)
            except Exception as e:   # a thread must report, not vanish
                r = {"ok": False, "model": model, "error": f"{type(e).__name__}: {e}"}
            if r["ok"]:
                ledger.warm(model)
            elif r.get("exit") in (7, 8, 9):
                ledger.cool(model, r["error"], busy=r.get("exit") == 8, permanent=r.get("exit") == 9)
            results.append(r)
            emit({"event": "answer" if r["ok"] else "error", **r})

        threads = [threading.Thread(target=one, args=(m,)) for m in pool]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    run_round(models, purse if paid_first else None)
    good = [r for r in results if r["ok"]]
    if not good and not paid_first and a.paid != "off" and not local:
        # Nothing came back: every free model was busy, gated or too slow. This is the case the
        # wallet exists for — one paid model, checked against the allowance first, rather than
        # handing back an empty panel. A local ask never falls back to the paid cloud models:
        # the whole point of picking a local provider is to stay off OpenRouter.
        allowed, why = purse.check() if purse else (False, "no paid allowance is configured")
        rescue = paid_models(c, min(MAX_PAID_PER_ASK, max(1, a.rescue_models))) if allowed else []
        emit({"event": "rescue", "models": rescue, "why": why,
              "wallet": purse.snapshot() if purse else None})
        if rescue:
            models += rescue
            run_round(rescue, purse)
            good = [r for r in results if r["ok"]]
    if len(good) >= 2 and not a.no_synthesis and not local:
        free_good = [r for r in good if not r.get("paid")]
        merger = pick_models(c, 1, None) or [(free_good or good)[0]["model"]]
        answers = "\n\n".join(f"--- {r['model']} ---\n{r['text'][:6000]}" for r in good)
        emit({"event": "start", "model": merger[0], "role": "synthesis"})
        sint_kw = dict(purse=purse if not merger[0].endswith(":free") else None)
        if provider:
            sint_kw["provider"] = provider
        r = run_flint(SYNTH.format(question=a.question.strip(), context=context, answers=answers),
                      repo, merger[0], a.steps, corpus_ok, a.timeout,
                      lambda s: emit({"event": "progress", "model": merger[0], "role": "synthesis", "text": s}),
                      **sint_kw)
        results.append(r)
        emit({"event": "synthesis" if r["ok"] else "error", "role": "synthesis", **r})
    emit({"event": "done", "answered": len(good), "asked": len(models),
          "requests": sum(r.get("requests", 0) for r in results),
          "usd": round(sum(r.get("usd") or 0.0 for r in results), 6),
          "wallet": purse.snapshot() if purse else None})
    return 0


# ------------------------------------------------------------------ other commands

def cmd_info(a):
    c = swarmd.load_cfg()
    db = Path(c.get("corpus_db") or "~/.flint/corpus.db").expanduser()
    corpus = None
    if db.is_file():
        import corpus_index
        corpus = corpus_index.stats(db)
    out = {"root": str(ROOT), "python": sys.executable, "config": str(swarmd.CONFIG),
           "config_kind": swarmd.config_kind(),
           "configured_repo": None if str(c.get("repo", "__")).startswith("__") else c.get("repo"),
           "models": swarmd.pool(c), "allow_paid": bool(c.get("allow_paid")), "corpus": corpus,
           "sandbox": sandbox.available(), "api_key": bool(os.environ.get("OPENROUTER_API_KEY")),
           "mit_experiment": c.get("mit_experiment"),
           "wallet": (lambda w: w.snapshot() if w else None)(wallet_for(c))}
    if a.quota:
        info = swarmd.account()
        out["quota"] = (info or {}).get("free_model_daily_requests")
        # These are key-wide provider totals, never an estimate for this swarm.
        out["provider_usage"] = {
            "available": info is not None,
            "source": "OpenRouter /api/v1/key", "scope": "configured API key",
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            **{k: (info or {}).get(k) for k in
               ("usage", "usage_daily", "usage_monthly", "limit_remaining")},
        }
    emit(out)
    return 0


def _journal_line(j):
    what = j.get("title") or j.get("model") or ""
    extra = j.get("stage") or j.get("role") or ""
    return f"{j.get('iso', '')[11:16]} {j.get('event', '')} {extra} {what}".strip()


def queue_row(t, rows, full=False):
    """One queued task as the editor shows it. `full` carries the whole detail, for the edit form;
    the list view only needs enough to recognise the task."""
    detail = (t.get("detail") or "").strip()
    row = {"id": t["id"], "title": t.get("title", ""), "kind": t.get("kind", "feature"),
           "priority": t.get("priority", 0), "attempts": t.get("attempts", 0),
           "claimed": bool(t.get("claimed")), "origin": t.get("origin"),
           "status": t.get("status"), "not_before": t.get("not_before", 0),
           "created": t.get("created", 0), "depth": t.get("depth", 0),
           "acceptance": [x for x in (t.get("acceptance") or []) if isinstance(x, str)],
           "depends_on": t.get("depends_on", []),
           "allow_test_changes": bool(t.get("allow_test_changes")),
           "harness_failures": t.get("harness_failures", 0),
           "blocks": [{"id": r["id"], "title": r["title"]} for r in swarmd.Queue.blocked_by(t["id"], rows)],
           "scope_gap": swarmd.scope_gap(t),
           "evidence": next((str(p / "HANDOFF.md") for p, _ in swarmd.attempts_for(t["id"])
                             if (p / "HANDOFF.md").is_file()), None),
           "note": (t.get("notes") or [t.get("note")] or [None])[-1]}
    row["detail"] = detail if full else detail[:400]
    row["detail_truncated"] = not full and len(detail) > 400
    return row


def _queue(repo):
    """(config, queue) for a repository, with per-repo state selected."""
    c = config_for(repo)
    swarmd.use_repo(c)
    return c, swarmd.Queue(c.get("max_depth", 1), c.get("max_queue", 20))


def cmd_queue_get(a):
    c, q = _queue(a.repo)
    rows = q.pending()
    t = next((r for r in rows if r["id"] == a.id), None)
    if t is None:
        emit({"ok": False, "error": f"no queued task with id {a.id}; it may have just been claimed"})
        return 2
    emit({"ok": True, "task": queue_row(t, rows, full=True), "kinds": list(swarmd.KINDS)})
    return 0


def cmd_queue_edit(a):
    c, q = _queue(a.repo)
    try:
        t = q.edit(a.id, title=a.title, detail=a.detail, kind=a.kind, priority=a.priority,
                   acceptance=a.acceptance, allow_test_changes=a.allow_test_changes)
    except KeyError:
        emit({"ok": False, "error": f"no queued task with id {a.id}"})
        return 2
    except ValueError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    emit({"ok": True, "task": queue_row(t, q.pending(), full=True)})
    return 0


def cmd_queue_retry(a):
    """Clear a task's retry backoff so the next free worker takes it."""
    c, q = _queue(a.repo)
    try:
        t = q.retry_now(a.id)
    except KeyError:
        emit({"ok": False, "error": f"no queued task with id {a.id}"})
        return 2
    emit({"ok": True, "task": queue_row(t, q.pending(), full=True)})
    return 0


def cmd_queue_requeue(a):
    """Put a parked or split task back in the queue, with its attempts cleared."""
    c, q = _queue(a.repo)
    try:
        t = q.requeue(a.id)
    except KeyError:
        emit({"ok": False, "error": f"no finished task with id {a.id}"})
        return 2
    except ValueError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    emit({"ok": True, "task": queue_row(t, q.pending(), full=True)})
    return 0


def cmd_import_bugs(a):
    """Turn new player bug reports into queued tasks."""
    c = config_for(a.repo)
    swarmd.use_repo(c)
    q = swarmd.Queue(c.get("max_depth", 1), c.get("max_queue", 20))
    try:
        added = swarmd.import_bugs(c, q, path=a.file, limit=a.limit or 5)
    except OSError as e:
        emit({"ok": False, "error": f"could not read the reports: {e}"})
        return 2
    emit({"ok": True, "repo": c["repo"], "added": len(added),
          "tasks": [{"id": t["id"], "title": t["title"], "priority": t["priority"]}
                    for t in added]})
    return 0


def cmd_landed(a):
    """The swarm's own trunk commits, newest first, with model, reward and goal item."""
    c = config_for(a.repo)
    swarmd.use_repo(c)
    emit({"ok": True, "repo": c["repo"], "trunk": swarmd.trunk_name(c),
          "commits": swarmd.landed_commits(c, a.limit or 20)})
    return 0


def cmd_parked(a):
    """Tasks the swarm gave up on, and whose failure each was."""
    c = config_for(a.repo)
    swarmd.use_repo(c)
    last = {j["id"]: j for j in swarmd._read(swarmd.STATE / "journal.jsonl")
            if j["event"] == "task"}
    rows = []
    for d in reversed(swarmd._read(swarmd.STATE / "done.jsonl")):
        if d.get("status") not in ("parked", "split") or d.get("dismissed_at"):
            continue
        j = last.get(d["id"], {})
        raw = j.get("note") or d.get("note") or ""
        rows.append({
            "id": d["id"], "title": d.get("title", ""), "status": d["status"],
            "finished": d.get("finished", 0), "attempts": d.get("attempts", 0),
            "harness_failures": d.get("harness_failures", 0),
            "stage": j.get("stage"),
            # With no recorded outcome there is nothing to classify, and guessing "task" is the
            # very mistake that split 45 tasks for failures that were never theirs.
            "failure_class": j.get("failure_class") or (swarmd.failure_class(
                j["stage"], raw, edited=j.get("edit_calls")) if j.get("stage") else None),
            "why": swarmd.reportable(raw)[:400],
            "handoff": next((str(p / "HANDOFF.md") for p in
                             sorted((swarmd.STATE / "attempts").glob(f"{d['id']}-*"), reverse=True)
                             if (p / "HANDOFF.md").is_file()), None)})
    emit({"ok": True, "repo": c["repo"], "total": len(rows), "parked": rows[:a.limit or 20]})
    return 0


def cmd_parked_dismiss(a):
    _, q = _queue(a.repo)
    try:
        ids = q.dismiss_finished(a.id)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return 2
    emit({"ok": True, "dismissed": len(ids), "ids": ids, "evidence_preserved": True})
    return 0


def cmd_queue_remove(a):
    c, q = _queue(a.repo)
    rows = q.pending()
    target = next((r for r in rows if r["id"] == a.id), None)
    try:
        gone = q.remove(a.id, cascade=a.cascade)
    except KeyError:
        emit({"ok": False, "error": f"no queued task with id {a.id}"})
        return 2
    except ValueError as e:
        emit({"ok": False, "error": str(e), "id": a.id,
              "needs_cascade": [{"id": r["id"], "title": r["title"]}
                                for r in swarmd.Queue.blocked_by(a.id, rows)]})
        return 2
    emit({"ok": True, "removed": [{"id": r["id"], "title": r["title"]} for r in gone],
          "was_claimed": bool(target and target.get("claimed")), "queued": len(q.pending())})
    return 0


def cmd_queue_clear(a):
    c, q = _queue(a.repo)
    gone, kept = q.clear(include_claimed=a.include_claimed)
    emit({"ok": True, "removed": [{"id": r["id"], "title": r["title"]} for r in gone],
          "kept": [{"id": r["id"], "title": r["title"]} for r in kept]})
    return 0


def cmd_wallet(a):
    """Read or set the editor's dollar allowance. The cap lives in swarm/config.json; what has
    been spent lives in ~/.flint/wallet.json, shared by every checkout on the machine."""
    c = swarmd.load_cfg()
    w = dict(c.get("editor_wallet") or {})
    if a.cap is not None:
        if a.cap < 0:
            emit({"ok": False, "error": "a cap cannot be negative"})
            return 2
        w["cap_usd"] = round(float(a.cap), 2)
    if a.enable:
        w["enabled"] = True
    if a.disable:
        w["enabled"] = False
    if a.cap is not None or a.enable or a.disable:
        c["editor_wallet"] = w
        swarmd.save_cfg(c)
    purse = wallet_for(c)
    if a.reset and purse:
        purse.reset()
    out = {"ok": True, "enabled": bool(purse), "config": str(swarmd.CONFIG),
           "wallet": purse.snapshot() if purse else
                     {"cap": 0.0, "spent": wallet.Wallet(cap=0).spent(), "remaining": 0.0},
           "paid_models": paid_models(c, 3) if purse else []}
    if a.account:
        info = swarmd.account() or {}
        out["account"] = {"is_free_tier": info.get("is_free_tier"),
                          "usage_daily": info.get("usage_daily"),
                          "limit_remaining": info.get("limit_remaining")}
    emit(out)
    return 0


def _local_models(base_url, timeout=2):
    """Models a local OpenAI-compatible server reports at GET /v1/models.

    Returns a list of (id, name, context, cost_hint) tuples — the fields the picker
    shows. Never raises: an unreachable server (Ollama not running, wrong port) is a
    per-provider list that says so instead of killing the whole dropdown.
    """
    if not base_url:
        return []
    import urllib.error
    import urllib.request
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        base = url if "/v1" in url else url + "/v1"
        url = base
    req = urllib.request.Request(url + "/models")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        rows = data.get("data") if isinstance(data, dict) else data
        out = []
        for m in rows or []:
            mid = m.get("id") or m.get("name") or ""
            if not mid:
                continue
            ctx = m.get("context_length") or (m.get("meta") or {}).get("context_length")
            out.append({"id": str(mid),
                        "name": m.get("name") or str(mid),
                        "context": ctx,
                        "created": m.get("created"),
                        "owned_by": m.get("owned_by")})
        return out
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return []


def _custom_endpoints():
    """User-added OpenAI-compatible endpoints from ~/.flint/providers.json, as
    {label, baseUrl, models} entries."""
    d = providers.load_providers()
    return d.get("customEndpoints") or []


def cmd_local_models(a):
    """List local server models for the editor's provider dropdown.

    Calls each local provider and this user's custom endpoints (all cheap GETs to
    /v1/models, each with a short timeout) and returns one JSON object with a
    `providers` array: {id, label, local, baseUrl, running, models:[{id,name,context}]}.
    The editor turns this into the provider → model QuickPick chain; a custom endpoint
    the user adds is persisted in ~/.flint/providers.json and listed here too.
    """
    out = {"providers": []}
    probe = getattr(a, "probe", False)
    for name, info in providers.PROVIDERS.items():
        if not info.get("local"):
            continue
        base = providers.base_url_for(name)
        models = _local_models(base) if probe else _local_models(base)
        out["providers"].append({
            "id": name, "label": info["label"], "local": True,
            "baseUrl": base, "running": bool(models),
            "models": [{k: m[k] for k in ("id", "name", "context") if k in m} for m in models],
        })
    for ep in _custom_endpoints():
        base = (ep.get("baseUrl") or "").strip()
        if not base:
            continue
        models = _local_models(base)
        out["providers"].append({
            "id": "custom:" + (ep.get("label") or base), "label": ep.get("label") or base,
            "local": True, "baseUrl": base, "running": bool(models),
            "models": [{k: m[k] for k in ("id", "name", "context") if k in m} for m in models],
        })
    emit(out)
    return 0


def _log_age():
    """Seconds since the daemon last wrote a line, or None if it has not written today.

    Silence is the only symptom a hung turn has, so the sidebar shows it next to the
    status rather than making someone open the log to find out."""
    path = swarmd.LOGS / f"{dt.date.today()}.log"
    try:
        return round(max(0.0, time.time() - path.stat().st_mtime), 1)
    except OSError:
        return None


def cmd_status(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    q = swarmd.Queue()
    pending = q.pending()
    done = swarmd._read(q.done)
    ahead = None
    if subprocess.run(["git", "-C", c["repo"], "rev-parse", "--verify", "-q", f"refs/heads/{swarmd.trunk_name(c)}"],
                      capture_output=True).returncode == 0:
        _, ahead = swarmd.git(["rev-list", "--count", f"{c.get('base_branch', 'main')}..{swarmd.trunk_name(c)}"], cwd=c["repo"])
    import experiment
    exp = experiment.summary(swarmd.STATE / "journal.jsonl")
    budget = swarmd.Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                           owner_window=c.get("owner_window", ["00:00", "00:00"])).snapshot()
    tuned = swarmd.per_repo_config(c["repo"])
    try:
        run = json.loads((swarmd.STATE / "run.json").read_text())
    except (OSError, ValueError):
        run = None
    emit({"repo": c["repo"], "is_target": is_target(c["repo"]), "daemon_running": daemon_running(),
          "now": swarmd.read_now(), "health": swarmd.health(24),
          "run": run,
          "config_path": str(swarmd.CONFIG), "config_kind": swarmd.config_kind(),
          "config_warnings": swarmd.config_warnings(c),
          "config_tuned_available": str(tuned) if tuned.is_file() else None,
          "trunk": swarmd.trunk_name(c), "trunk_ahead": int(ahead) if str(ahead or "").isdigit() else None,
          # A roadmap run whose packets are all held looks exactly like an idle one from the
          # queue alone. The report says which packet is held, why, and what releases it.
          "roadmap": swarmd.read_roadmap() if c.get("roadmap_file") else None,
          # Spare-time work: what it last found, and what is standing it down right now.
          "idle": swarmd.idle_state(),
          "queue": [queue_row(t, pending) for t in pending],
          "max_queue": c.get("max_queue", 20),
          "kinds": list(swarmd.KINDS),
          "landed": sum(t.get("status") == "done" for t in done),
          "parked": sum(t.get("status") in ("parked", "split") and not t.get("dismissed_at") for t in done),
          "budget": budget, "experiment": {"verdict": exp["verdict"], "on": exp["on"]["attempts"],
                                           "off": exp["off"]["attempts"]},
          "spend": {"used": round(swarmd.spend_used(c), 6), "cap": swarmd.spend_cap(c),
                    "left": swarmd.spend_left(c), "resets": swarmd.spend_resets(c),
                    "shared": swarmd.monthly(c)},
          "stale_seconds": _log_age(),
          "wallet": (lambda w: w.snapshot() if w else None)(wallet_for(c)),
          "recent": [_journal_line(j) for j in swarmd._read(swarmd.STATE / "journal.jsonl")[-12:]]})
    return 0


def cmd_task(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    where, code = selection_context(c["repo"], a.file, a.start, a.end, a.selection_file)
    want = (a.detail or a.title).strip()
    detail = want
    if where:
        detail += f"\n\nThe developer pointed at {where}:\n```\n{code[:4000]}\n```"
    draft = {"title": a.title.strip(), "detail": detail, "kind": a.kind,
             "priority": a.priority, "acceptance": [want], "origin": "cursor"}
    # The same gate the worker applies before it spends anything, run here so the answer
    # arrives while the developer is still looking at the form rather than an hour later as
    # a parked task. A question typed into an editor is exactly the kind of request that
    # reaches an implementer with nothing in it to aim at.
    gap = swarmd.scope_gap(draft, c)
    q = swarmd.Queue(c.get("max_depth", 1), c.get("max_queue", 20))
    if a.preview:
        pending = q.pending()
        seen = {r["title"].strip().lower() for r in pending + swarmd._read(q.done)}
        emit({"ok": True, "preview": True, "repo": c["repo"], "task": draft, "where": where,
              "scope_gap": gap, "duplicate": draft["title"].lower() in seen,
              "queued": len(pending), "max_queue": c.get("max_queue", 20),
              "queue_full": len(pending) >= c.get("max_queue", 20),
              "test_cmd": c.get("test_cmd"), "trunk": swarmd.trunk_name(c),
              "is_target": is_target(c["repo"]), "daemon_running": daemon_running()})
        return 0
    try:
        t = q.add(a.title, detail, a.kind, priority=a.priority, origin="cursor",
                  acceptance=[want])
    except ValueError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    emit({"ok": bool(t), "id": t and t["id"], "duplicate_or_full": not t, "scope_gap": gap,
          "is_target": is_target(c["repo"]), "daemon_running": daemon_running()})
    return 0


def cmd_evidence(a):
    """What was recorded about one task's attempts, newest first.

    The panel had an evidence link for parked tasks that never resolved: it looked for
    `attempts/<task id>-*` and the daemon wrote `attempts/<worker>`, overwriting it on the
    next task. Both ends now agree, and this is what the link opens."""
    c = config_for(a.repo)
    swarmd.use_repo(c)
    rows = []
    for path, data in swarmd.attempts_for(a.id):
        files = sorted(f.name for f in path.iterdir() if f.is_file()) if path.is_dir() else []
        rows.append({
            "attempt": data.get("id") or path.name, "dir": str(path),
            "phase": data.get("phase"), "note": (data.get("note") or "")[:600],
            "started": data.get("started"), "finished": data.get("finished"),
            "seconds": data.get("seconds"), "branch": data.get("branch"),
            "commit": data.get("commit"), "strategy": data.get("strategy"),
            "role_calls": data.get("role_calls"), "repairs": data.get("repairs"),
            "reviewer": data.get("reviewer"), "reviewed_tree": data.get("reviewed_tree"),
            "handoff": str(path / "HANDOFF.md") if (path / "HANDOFF.md").is_file() else None,
            "files": [{"name": n, "path": str(path / n)} for n in files]})
    emit({"ok": True, "repo": c["repo"], "id": a.id, "attempts": rows})
    return 0


def cmd_study(a):
    import mit_corpus
    emit({"query": a.query, "hits": mit_corpus.search(a.query, a.k, max_chars=a.chars)})
    return 0


def cmd_report(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    try:
        text = swarmd.build_report(c, a.hours)
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 2
    emit({"ok": True, "markdown": text})
    return 0


def cmd_vote(a):
    CONSULT.parent.mkdir(parents=True, exist_ok=True)
    Ledger(CONSULT).update("consult", a.model, 1.0 if a.useful else 0.0)
    emit({"ok": True, "model": a.model, "useful": bool(a.useful),
          "leaderboard": [r for r in Ledger(CONSULT).leaderboard() if r["role"] == "consult"][:10]})
    return 0


def cmd_set_key(a):
    """Write OPENROUTER_API_KEY into the checkout .env (mode 0600).

    The Cursor extension calls this from its fresh-start key prompt; the key is written by
    the bridge (which already owns the .env contract) rather than by the extension process,
    so the secret never rides through the webview or the extension's settings pipe.
    """
    key = (getattr(a, "key", "") or "").strip()
    if a.check and not key:
        emit({"ok": True, "set": bool(os.environ.get("OPENROUTER_API_KEY")),
              "path": str(ROOT / ".env")})
        return 0
    if not key.startswith("sk-or-") and not key.startswith("sk-"):
        emit({"event": "error", "ok": False,
              "error": "that does not look like an OpenRouter key (starts with sk-or- or sk-)",
              "prefix": key[:6]})
        return 2
    env_path = ROOT / ".env"
    try:
        lines = []
        if env_path.is_file():
            lines = env_path.read_text().splitlines()
        kept = [ln for ln in lines if not ln.lstrip().startswith("OPENROUTER_API_KEY=")]
        kept.append(f"OPENROUTER_API_KEY={key}")
        tmp = env_path.with_name(env_path.name + ".tmp")
        tmp.write_text("\n".join(kept) + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(env_path)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        os.environ["OPENROUTER_API_KEY"] = key
        emit({"ok": True, "set": True, "path": str(env_path)})
        return 0
    except OSError as e:
        emit({"event": "error", "ok": False, "error": f"could not write {env_path}: {e}"})
        return 1


def cmd_extension(a):
    """Manage the extensions runtime (Phase 3): list, trust, run."""
    import extensions
    if a.list:
        emit({"ok": True, "extensions": extensions._read_manifest(),
              "trusted": sorted(extensions._trusted()), "root": str(extensions.ext_root())})
        return 0
    if a.trust:
        path = extensions.trust(a.trust)
        if not path:
            emit({"event": "error", "ok": False,
                  "error": f"no extension named '{a.trust}' in {extensions.ext_root()}"})
            return 2
        emit({"ok": True, "trusted": a.trust, "path": path})
        return 0
    if a.run and a.event:
        results = extensions.run_extensions([a.event], ROOT, {})
        emit({"ok": True, "results": results})
        return 0
    emit({"event": "error", "ok": False, "error": "extension needs --list, --trust NAME or --run --event EVENT"})
    return 2


def cmd_doctor(a):
    """Hardware-aware local-model recommendation (albatross `/doctor` parity)."""
    import hardware
    emit(hardware.doctor())
    return 0


def cmd_route(a):
    """Score the configured model pool for a task and say which model to use (Phase 2)."""
    import routing
    import providers as _providers
    c = config_for(a.repo) if getattr(a, "repo", None) else swarmd.load_cfg()
    models = [m for m in (c.get("models") or []) if isinstance(m, str)]
    catalog = None
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            catalog = _catalog()
        except Exception:
            catalog = None
    policy = _providers.load_providers().get("modelSystem", {}).get("policy") or {}
    policy = {**policy, **({k: v for k, v in (getattr(a, "policy", None) or {}).items() if v is not None})}
    if getattr(a, "local_only", False):
        policy["localOnly"] = True
    if getattr(a, "max_turn", None):
        policy["maxTurnUsd"] = a.max_turn
    task = " ".join(a.task or []) or None
    if getattr(a, "select", False):
        pick = routing.select(models, catalog=catalog, policy=policy, task=task)
        emit({"ok": pick is not None, "choice": pick, "policy": policy,
              "pool_size": len(models)})
    else:
        emit({"ok": True, "ranked": routing.rank(models, catalog=catalog, policy=policy, task=task),
              "policy": policy, "pool_size": len(models)})
    return 0


def cmd_receipts(a):
    """The lasting cost ledger (~/.albatross/routes.jsonl): totals and recent rows."""
    import receipts
    hours = getattr(a, "hours", 24)
    since = None if not hours else time.time() - hours * 3600
    out = {"path": str(receipts.routes_path()), "tail": receipts.tail(limit=getattr(a, "limit", 20))}
    out["summary"] = receipts.summarize(since=since)
    if getattr(a, "days", None):
        out["days"] = _receipt_days(getattr(a, "days", 7))
    emit(out)
    return 0


def _receipt_days(n):
    """Per-day spend for the last n days, for the panel's cost footer."""
    import receipts
    days = []
    for i in range(n):
        start = time.time() - (i + 1) * 86400
        end = time.time() - i * 86400
        rows = [r for r in receipts._all() if start <= (r.get("at") or 0) < end]
        days.append({"date": dt.date.fromtimestamp(start).isoformat(),
                     "usd": round(sum(max(0.0, float(r.get("usd") or 0)) for r in rows), 6),
                     "requests": len(rows)})
    return days


def cmd_undo(a):
    """Undo a flint turn's file changes (Phase 2): `bridge.py undo --session <id> [--dry-run]`."""
    import undo as undo_mod
    if getattr(a, "list", False):
        emit({"ok": True, "sessions": undo_mod.list_sessions()})
        return 0
    if not a.session:
        emit({"event": "error", "ok": False, "error": "undo needs --session <id> (use --list to see them)"})
        return 2
    restored, skipped = undo_mod.undo(a.session, dry_run=bool(a.dry_run))
    emit({"ok": True, "dry_run": bool(a.dry_run), "session": a.session,
          "restored": [{"path": p, "bytes": b} for p, b in restored],
          "skipped": [{"path": p, "why": w} for p, w in skipped]})
    return 0


def cmd_continue(a):
    """Resume the last nonstop session's goal for a few more hours: `bridge.py continue [--hours N]`.

    The daemon keeps no transcript to replay — the checkpoint material is a handoff for the
    next request, not a resumable conversation — so "continue" means: start another nonstop
    run on the same goal with the same model, which is exactly what the session handoff is
    designed for. When a goal is given it replaces the previous one.
    """
    state_dir = Path(os.environ.get("FLINT_HOME", "~/.flint")).expanduser()
    sessions = sorted((state_dir / "nonstop").glob("*/status.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True) if (state_dir / "nonstop").is_dir() else []
    if not sessions and not a.goal:
        emit({"event": "error", "ok": False, "error": "no previous nonstop session; give --goal G"})
        return 1
    goal = a.goal
    model = a.model
    if sessions:
        state = json.loads(sessions[0].read_text())
        goal = goal or state.get("goal")
        model = model or state.get("model")
    if not goal:
        emit({"event": "error", "ok": False, "error": "no goal known; give --goal G"})
        return 1
    hours = a.hours or 4
    emit({"ok": True, "goal": goal[:200], "model": model, "hours": hours,
          "previous": str(sessions[0]) if sessions else None,
          "style": "resume the previous session's goal with a new run"})
    return 0


def cmd_grind_cmd(a):
    repo = str(Path(a.repo).expanduser().resolve())
    parts = [sys.executable, str(HERE / "swarmd.py"), "grind", repo]
    if a.goal:
        parts += ["--goal", a.goal]
    if a.test_cmd:
        parts += ["--test-cmd", a.test_cmd]
    if a.hours:
        parts += ["--hours", str(a.hours)]
    command = " ".join(shlex.quote(p) for p in parts)
    # swarmd resolves this itself from the repo argument; naming it here makes the line the
    # panel drops into a terminal say out loud which settings it is starting on.
    tuned = swarmd.per_repo_config(repo)
    config = str(swarmd.config_path_for(repo))
    if tuned.is_file():
        command = f"FLINT_SWARM_CONFIG={shlex.quote(str(tuned))} " + command
    emit({"command": command, "config": config, "config_kind": swarmd.config_kind(config)})
    return 0


def cmd_activity(a):
    """The daemon's own log for this repo, tailed by byte offset.

    swarmd.log() appends every line the daemon prints to LOGS/<date>.log, and that is the
    only place a stall shows up: the journal records task boundaries, so a hung turn and a
    quiet one look identical there. A hang is the absence of output, so the caller needs
    the file's age as much as its new lines — `stale_seconds` is the point of this call.

    The state slug is a sha1 of the repo path built in swarmd.use_repo, so the caller
    never has to know it; it asks by repo and the bridge finds the log.
    """
    c = config_for(a.repo)
    swarmd.use_repo(c)
    day = a.day or dt.date.today().isoformat()
    path = swarmd.LOGS / f"{day}.log"
    out = {"repo": c["repo"], "day": day, "path": str(path), "exists": path.exists(),
           "daemon_running": daemon_running(), "lines": [], "offset": 0, "stale_seconds": None}
    if not path.exists():
        emit(out)                      # no daemon has run today; not an error
        return 0
    size = path.stat().st_size
    start = max(0, int(a.offset or 0))
    if start > size:
        start = 0                      # the file was replaced or truncated under us
    with path.open("rb") as f:
        f.seek(start)
        blob = f.read()
    # Hold back a partial last line instead of showing half of one; it arrives next call.
    cut = blob.rfind(b"\n") + 1
    out["offset"] = start + cut
    out["lines"] = blob[:cut].decode("utf-8", "replace").splitlines()
    out["stale_seconds"] = round(max(0.0, time.time() - path.stat().st_mtime), 1)
    emit(out)
    return 0


def proc_args(pid):
    """That pid's command line, or "" when it is gone."""
    return subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                          capture_output=True, text=True).stdout.strip()


def stop_every_swarm():
    """SIGINT to every swarm daemon on this machine, whatever repository it works on."""
    out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True).stdout
    me, stopped = os.getpid(), []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != me and re.search(r"swarmd\.py\s+(grind|run)\b", args):
            try:
                os.kill(int(pid), signal.SIGINT)
                stopped.append(int(pid))
            except (ProcessLookupError, PermissionError):
                pass
    emit({"ok": True, "scope": "all", "stopped": stopped})
    return 0


def cmd_stop(a):
    """SIGINT to one repository's swarm daemon (the same as Ctrl-C in its terminal).

    The pid comes from that repository's own STATE/daemon.pid, written by the daemon while it
    holds the repository's lock, and is signalled only after ps confirms it is still a swarm
    for this repository. The machine-wide sweep is now `--all` and nothing else: as the default
    it meant the Stop button on the studio also stopped the hedge-fund and kraken swarms.
    """
    if a.all:
        return stop_every_swarm()
    if not a.repo:
        emit({"ok": False, "error": "stop needs --repo, or --all to stop every swarm on this machine"})
        return 2
    c = config_for(a.repo)
    swarmd.use_repo(c)
    repo = str(Path(c["repo"]).expanduser().resolve())
    note = swarmd.daemon_note()          # None once the process that wrote it is gone
    if not note:
        emit({"ok": True, "repo": repo, "stopped": [],
              "reason": "no swarm daemon is running on this repository"})
        return 0
    pid, args = int(note["pid"]), proc_args(int(note["pid"]))
    how = "drain" if getattr(a, "drain", False) else "stop"
    noted = str(note.get("repo") or "")
    # A pid file outlives the run that wrote it, so the pid may belong to something else now.
    if "swarmd.py" not in args or (noted and noted != repo) or (not noted and repo not in args):
        emit({"ok": False, "repo": repo, "pid": pid, "stopped": [],
              "error": f"pid {pid} is not this repository's swarm daemon; not signalling it"})
        return 2
    try:
        # A drain lets the task in flight finish; SIGINT kills it where it stands.
        os.kill(pid, signal.SIGUSR1 if how == "drain" else signal.SIGINT)
    except (ProcessLookupError, PermissionError) as e:
        emit({"ok": False, "repo": repo, "pid": pid, "stopped": [],
              "error": f"{type(e).__name__}: {e}"})
        return 2
    emit({"ok": True, "repo": repo, "how": how,
          "stopped": [] if how == "drain" else [pid], "draining": [pid] if how == "drain" else []})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info")
    s.add_argument("--quota", action="store_true", help="also ask OpenRouter for today's free-request usage")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("status")
    s.add_argument("--repo", required=True)
    s.set_defaults(fn=cmd_status)
    for name, fn in (("ask", cmd_ask), ("task", cmd_task)):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--file")
        s.add_argument("--start", type=int)
        s.add_argument("--end", type=int)
        s.add_argument("--selection-file")
        s.set_defaults(fn=fn)
        if name == "ask":
            s.add_argument("--question", required=True)
            s.add_argument("--models", type=int, default=3)
            s.add_argument("--model", help="comma-separated model ids instead of sampling")
            s.add_argument("--provider", default=None,
                           help="backend for this ask (ollama, lm-studio, mlx, llamacpp, openai, "
                                "anthropic, openrouter). A local provider skips the wallet and the "
                                "OpenRouter request budget entirely.")
            s.add_argument("--base-url", default=None, dest="base_url",
                           help="endpoint override for the provider; with --provider a custom "
                                "OpenAI-compatible URL is written to ~/.flint/providers.json")
            s.add_argument("--steps", type=int, default=8)
            s.add_argument("--timeout", type=int, default=420, help="seconds per model (free models can be slow)")
            s.add_argument("--no-synthesis", action="store_true")
            s.add_argument("--no-study", action="store_true")
            s.add_argument("--paid", choices=("off", "auto", "always"), default="auto",
                           help="off: free models only. auto: spend from the editor wallet only "
                                "when no free model answered. always: start with paid models.")
            s.add_argument("--rescue-models", type=int, default=1,
                           help=f"paid models to try when no free one answered (max {MAX_PAID_PER_ASK})")
        else:
            s.add_argument("--title", required=True)
            s.add_argument("--detail", default="")
            s.add_argument("--kind", default="feature", choices=swarmd.KINDS)
            s.add_argument("--priority", type=int, default=1)
            s.add_argument("--preview", action="store_true",
                           help="say what would be queued, and whether it is workable, "
                                "without queueing it")
    s = sub.add_parser("evidence")
    s.add_argument("--repo", required=True)
    s.add_argument("--id", required=True)
    s.set_defaults(fn=cmd_evidence)
    for name, fn in (("landed", cmd_landed), ("parked", cmd_parked)):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--limit", type=int, default=20)
        s.set_defaults(fn=fn)
    s = sub.add_parser("parked-dismiss", help="dismiss abandoned tasks without deleting evidence")
    s.add_argument("--repo", required=True)
    choice = s.add_mutually_exclusive_group(required=True)
    choice.add_argument("--id")
    choice.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_parked_dismiss)
    s = sub.add_parser("import-bugs")
    s.add_argument("--repo", required=True)
    s.add_argument("--limit", type=int, default=5, help="at most this many per run (default 5)")
    s.add_argument("--file", help="the reports file (default <repo>/reports/bugs.jsonl)")
    s.set_defaults(fn=cmd_import_bugs)
    for name, fn in (("queue-get", cmd_queue_get), ("queue-edit", cmd_queue_edit),
                     ("queue-retry", cmd_queue_retry), ("queue-requeue", cmd_queue_requeue),
                     ("queue-remove", cmd_queue_remove)):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--id", required=True)
        s.set_defaults(fn=fn)
        if name == "queue-edit":
            s.add_argument("--title")
            s.add_argument("--detail")
            s.add_argument("--kind", choices=swarmd.KINDS)
            s.add_argument("--priority", type=int, choices=(0, 1, 2))
            s.add_argument("--acceptance", action="append",
                           help="one acceptance criterion; repeat for several")
            # Editing the queue is a person's doing, so this is theirs to grant or take back.
            s.add_argument("--allow-test-changes", dest="allow_test_changes",
                           action="store_true", default=None,
                           help="let this task change or delete existing tests")
            s.add_argument("--no-allow-test-changes", dest="allow_test_changes",
                           action="store_false", help="take that permission back")
        if name == "queue-remove":
            s.add_argument("--cascade", action="store_true",
                           help="also remove the queued tasks that depend on this one")
    s = sub.add_parser("queue-clear")
    s.add_argument("--repo", required=True)
    s.add_argument("--include-claimed", action="store_true",
                   help="also drop the task a worker is running right now")
    s.set_defaults(fn=cmd_queue_clear)
    s = sub.add_parser("wallet")
    s.add_argument("--cap", type=float, help="dollars the editor may spend in total (0 disables it)")
    s.add_argument("--reset", action="store_true", help="set what has been spent back to $0")
    s.add_argument("--enable", action="store_true")
    s.add_argument("--disable", action="store_true")
    s.add_argument("--account", action="store_true", help="also ask OpenRouter about the account's credits")
    s.set_defaults(fn=cmd_wallet)
    s = sub.add_parser("local-models", help="list local servers' models for the editor's provider dropdown")
    s.add_argument("--probe", action="store_true",
                   help="probe every local provider even when it looks quiet (default: probe only)")
    s.set_defaults(fn=cmd_local_models)
    s = sub.add_parser("study")
    s.add_argument("--query", required=True)
    s.add_argument("-k", type=int, default=6)
    s.add_argument("--chars", type=int, default=1600)
    s.set_defaults(fn=cmd_study)
    s = sub.add_parser("set-key", help="write OPENROUTER_API_KEY to the checkout .env (0600)")
    s.add_argument("--key", default="", help="the key to store (sk-or-... or sk-...)")
    s.add_argument("--check", action="store_true",
                   help="say whether a key is already set, without writing anything")
    s.set_defaults(fn=cmd_set_key)
    s = sub.add_parser("extension", help="manage the extensions runtime (~/.flint/extensions)")
    s.add_argument("--list", action="store_true", help="list installed extensions and their trust state")
    s.add_argument("--trust", metavar="NAME", help="trust an extension by name (records its hash)")
    s.add_argument("--run", action="store_true", help="run the extensions registered for --event")
    s.add_argument("--event", default="PostToolUse", help="event to fire (default PostToolUse)")
    s.set_defaults(fn=cmd_extension)
    s = sub.add_parser("doctor", help="hardware-aware local-model recommendation (albatross /doctor parity)")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("route", help="score the model pool for a task and pick the best (Phase 2)")
    s.add_argument("--repo", help="repository whose config pool to score (default: default config)")
    s.add_argument("--task", nargs="*", default=None, help="the task text to score against")
    s.add_argument("--select", action="store_true", help="emit just the best choice")
    s.add_argument("--local-only", action="store_true")
    s.add_argument("--max-turn", type=float, help="refuse candidates costing more than this per turn")
    s.set_defaults(fn=cmd_route)
    s = sub.add_parser("receipts", help="the lasting cost ledger (~/.albatross/routes.jsonl)")
    s.add_argument("--hours", type=float, default=24, help="summary window in hours (0 = all)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--days", type=int, default=None, help="also emit per-day spend for this many days")
    s.set_defaults(fn=cmd_receipts)
    s = sub.add_parser("undo", help="undo a flint turn's file changes (Phase 2)")
    s.add_argument("--session", help="the undo session id (a flint turn)")
    s.add_argument("--list", action="store_true", help="list recent sessions with an undo log")
    s.add_argument("--dry-run", action="store_true", help="say what would be restored, change nothing")
    s.set_defaults(fn=cmd_undo)
    s = sub.add_parser("continue", help="resume the last nonstop session's goal (albatross --continue parity)")
    s.add_argument("--goal", default="", help="replace the previous goal (default: keep it)")
    s.add_argument("--model", default="", help="model for the continued run (default: the one it used)")
    s.add_argument("--hours", type=float, default=4, help="hours to run for (default 4)")
    s.set_defaults(fn=cmd_continue)
    s = sub.add_parser("report")
    s.add_argument("--repo", required=True)
    s.add_argument("--hours", type=float, default=24)
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("vote")
    s.add_argument("--model", required=True)
    s.add_argument("--useful", type=int, choices=(0, 1), required=True)
    s.set_defaults(fn=cmd_vote)
    s = sub.add_parser("grind-cmd")
    s.add_argument("--repo", required=True)
    s.add_argument("--goal")
    s.add_argument("--test-cmd")
    s.add_argument("--hours", type=float)
    s.set_defaults(fn=cmd_grind_cmd)
    s = sub.add_parser("activity")
    s.add_argument("--repo", required=True)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--day", help="a past day's log (default today)")
    s.set_defaults(fn=cmd_activity)
    s = sub.add_parser("stop")
    s.add_argument("--repo", help="the repository whose daemon to stop")
    s.add_argument("--drain", action="store_true",
                   help="finish the task in flight first, instead of killing it mid-turn")
    s.add_argument("--all", action="store_true",
                   help="stop every swarm daemon on this machine, whatever repository it works on")
    s.set_defaults(fn=cmd_stop)
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _kill_children)
    try:
        return a.fn(a)
    except SystemExit:
        raise
    except Exception as e:
        emit({"event": "error", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
