"""The swarm daemon's command line: sub-commands, their flags and help text.

Parsing only. Each sub-command is bound to a handler supplied by the caller, so this
module imports no daemon code.
"""
from __future__ import annotations

import argparse


def build_parser(handlers, description=None):
    """The `swarm` argument parser, with each sub-command's `fn` taken from `handlers`
    (a mapping of sub-command name -> callable taking the parsed args)."""
    ap = argparse.ArgumentParser(description=description,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grind", help="run nonstop until Ctrl-C (keeps the Mac awake)")
    g.add_argument("repo", nargs="?", help="target Git repository (default: configured one)")
    g.add_argument("--goal", help="what the project should become (stored with the swarm's state)")
    g.add_argument("--new", metavar="DIR", help="start a new project in DIR for a problem with no codebase")
    g.add_argument("--test-cmd", help="command that must pass for work to land")
    g.add_argument("--hours", type=float, help="optional limit; default is no limit")
    g.add_argument("--workers", type=int)
    g.add_argument("--max-tasks", type=int, help="stop after finishing this many tasks")
    g.set_defaults(fn=handlers["grind"])
    run = sub.add_parser("run", help="bounded run")
    run.add_argument("--repo", help="target Git repository (default: configured one)")
    run.add_argument("--hours", type=float, default=24, help="stop after this many hours (default 24)")
    run.add_argument("--workers", type=int)
    run.add_argument("--max-tasks", type=int)
    run.set_defaults(fn=handlers["run"])
    init = sub.add_parser("init", help="write a tuned config + prov hooks for a repository (see --check)")
    init.add_argument("repo", nargs="?", help="target Git repository")
    init.add_argument("-n", "--check", action="store_true",
                      help="print the repository's surroundings (nested repos, runtime deps, "
                           "venv, node_modules) without writing a config")
    init.set_defaults(fn=handlers["init"])
    st = sub.add_parser("status", help="what is running and where")
    st.add_argument("--repo", help="target Git repository (default: configured one)")
    st.set_defaults(fn=handlers["status"])
    r = sub.add_parser("report", help="what landed, breakthroughs, leaderboard, playbook")
    r.add_argument("--repo", help="target Git repository (default: configured one)")
    r.add_argument("--hours", type=float, default=24)
    r.set_defaults(fn=handlers["report"])
    p = sub.add_parser("add")
    p.add_argument("--repo", help="target Git repository (default: configured one)")
    p.add_argument("title")
    p.add_argument("--detail", default="")
    p.add_argument("--kind", default="feature")
    p.add_argument("--priority", type=int, default=1, help="hand-added tasks jump the queue (default 1)")
    p.add_argument("--execution-class", choices=("standard", "fast"), default="standard",
                   help="fast: explicit mechanical edit; no study/history, at most 4 rounds and 300s")
    p.add_argument("--acceptance", action="append", metavar="TEXT",
                   help="one thing the reviewer must verify; repeat for each (default: the detail)")
    p.add_argument("--depends-on", action="append", metavar="TITLE_OR_ID", dest="depends_on",
                   help="a task that must finish first, by exact title or id; repeat for each")
    p.add_argument("--allow-test-changes", action="store_true", dest="allow_test_changes",
                   help="this task may change or delete existing tests (e.g. removing a game "
                        "and its tests). Only you can set this; the adversary is told.")
    p.set_defaults(fn=handlers["add"])
    p2 = sub.add_parser("plan")
    p2.add_argument("--repo", help="target Git repository (default: configured one)")
    p2.add_argument("-n", type=int, default=5)
    p2.set_defaults(fn=handlers["plan"])
    sv = sub.add_parser("service", help="a launchd agent that restarts this repository's swarm")
    sv.add_argument("action", choices=("install", "status", "uninstall"))
    sv.add_argument("--repo", help="the repository to grind (default: the configured one)")
    sv.add_argument("--hours", type=float, help="stop after this many hours per run")
    sv.add_argument("--goal", default="GOAL.md")
    sv.add_argument("--autostart", action="store_true",
                    help="also start at login and after a reboot. This daemon spends money, so "
                         "it is off unless you ask for it.")
    sv.set_defaults(fn=handlers["service"])
    st = sub.add_parser("stop", help="stop this repository's daemon")
    st.add_argument("--drain", action="store_true",
                    help="finish the task(s) in flight first, instead of killing them mid-turn")
    st.set_defaults(fn=handlers["stop"])
    sub.add_parser("wake", help="end every model's rest now").set_defaults(fn=handlers["wake"])
    m = sub.add_parser("models", help="list free tool-capable models; --write sets the pool")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=handlers["models"])
    ex = sub.add_parser("exceptions",
                        help="show exceptions the harness swallowed (swallow.py recorder + journal errors)")
    ex.set_defaults(fn=handlers["exceptions"])
    return ap
