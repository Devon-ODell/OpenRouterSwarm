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
