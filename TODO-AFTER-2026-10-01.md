# Deferred until after 2026-10-01

Owner asked on 2026-09-27 for these to wait. Nothing here is urgent; each has a
workaround that is in use now.

## 1. ~~The Cursor panel's Stop button errors instead of stopping~~ — FIXED 2026-09-27

Fixed rather than deferred, because it kept not working: the extension was
reinstalled as **0.4.0** and Cursor now has that version registered. Reload the
window (`Developer: Reload Window`) if you have not since. The rest of this
entry is kept as the record of what was wrong.

### What it was

**What you see.** Pressing Stop reports that `--repo` or `--all` is required.

**Why.** `bridge.py stop` became per-repository on 2026-09-26 (commit "Stop one
swarm, not every swarm on the machine"): it used to SIGINT every swarm process
on the machine, so stopping the studio also stopped the hedge-fund and kraken
swarms. It now requires `--repo`, with `--all` for the old sweep.

The extension source was updated in the same commit — `panel.js` sends the
repository it is showing and `extension.js` passes `--repo` — but **the
installed copy of the extension is older than that change**, so it still calls
`bridge.py stop` bare.

**The fix is a reinstall, not a code change:**

```sh
cd /Users/devonodell/Desktop/OpenRouterSwarm
./cursor-extension/install.sh
# then: Cursor → Developer: Reload Window
```

Worth checking after the reload: Stop now, Stop after this task, and Restart are
three separate buttons in the updated panel, and the Queue and Landed tabs are
new. If Stop still errors after reinstalling, the panel is sending no repo —
look at the `stop` branch of `onMessage` in `cursor-extension/extension.js`.

**Until then, from a terminal:**

```sh
cd /Users/devonodell/Desktop/OpenRouterSwarm
STUDIO=/Users/devonodell/Desktop/internetmoney/video-games/openRouter-Studio
./.venv/bin/python swarm/bridge.py stop --repo $STUDIO --drain   # finish the task first
./.venv/bin/python swarm/bridge.py stop --repo $STUDIO           # stop now
```

Prefer `--drain`: it lets the task in flight finish instead of throwing away a
turn that has already been paid for.


## 2. ~~`swarm add` has no `--repo`, so it trusts whatever config.json points at~~ — FIXED 2026-10-02

Fixed: `swarmd.py` now accepts `--repo` on `add`, `plan`, `report`, `status` and
`run`, and `_setup(a)` calls `use_config(a.repo)` before loading, exactly as
`cmd_grind` already did. `swarm add --repo <studio> ...` is now ground on the
repository's own tuned file (`swarm/configs/<slug>.json`), so its `max_queue`,
`max_depth`, validation limits and paid fallback are the tuned ones, and the
`config_mismatch` warning no longer fires. Three tests in
`tests/test_config_resolution.py` cover it: `_setup --repo` uses the tuned file,
`_setup` without stays on the default, and `add --repo` enforces the tuned
`max_queue`. Gate: 780 passed + 26 subtests.

Still works, and still the documented equivalent for other entry points:

```sh
FLINT_SWARM_CONFIG=/Users/devonodell/Desktop/OpenRouterSwarm/swarm/configs/openRouter-Studio-ab0a4a.json \
  ./.venv/bin/python swarm/swarmd.py add "<title>" --detail "..." --priority 2
```

### What it was

`swarm add` resolves its config through `cfg()` with no repository argument, so it
writes to whichever repo `swarm/config.json` names and uses that file's settings —
`max_queue`, `max_depth`, the validation limits — even when the target repository
has a tuned config of its own.

It is not silent: the per-repo resolution added on 2026-09-26 logs

    WARNING using .../swarm/config.json while openRouter-Studio-ab0a4a.json exists
    for this repo — its tuned settings are not in effect

and journals `config_mismatch`. That warning fired while dispatching the roadmap's
T-* packets on 2026-09-27. The tasks landed in the right queue only because
config.json happened to name the studio.

**Workaround, and what the roadmap already tells you to do:** prefix the dispatch
command with the config, which is what `docs/roadmap/README.md` §8 prints:

```sh
FLINT_SWARM_CONFIG=/Users/devonodell/Desktop/OpenRouterSwarm/swarm/configs/openRouter-Studio-ab0a4a.json \
  ./.venv/bin/python swarm/swarmd.py add "<title>" --detail "..." --priority 2
```

**The fix:** give `cmd_add` (and `plan`, `report`, `status`) a `--repo` and call
`use_config(a.repo)` before `_setup()`, the way `cmd_grind` already does. About
four lines plus a test that `add --repo <studio>` uses the tuned `max_queue`.
