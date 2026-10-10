# COMMAND — operating doctrine for OpenRouterSwarm

*Last updated: 2026-10-02*

This file is the crystallization point for the whole project. When in doubt
about whether something belongs, read this file first.

## The principle

Everything this project ships exists to maximize the power of **free AI**.

The September benchmark proved the shape of that game:

| model                              | tasks | spend  | total time |
|------------------------------------|-------|--------|-----------|
| `dots-studio/dots-3-note-preview:free` | 14/14 | $0.000 | 224s |
| `anthropic/claude-haiku-4.5`       | 14/14 | $0.221 | 277s |
| `openai/gpt-5-mini`                | 14/15 | $0.039 | 278s |
| `qwen/qwen3-coder`                 | 13/14 | $0.153 | 313s |
| `anthropic/claude-sonnet-5.5`      | 13/14 | $0.267 | 168s |
| `google/gemini-2.5-flash`          | 10/14 | $0.026 | 127s |

A $0 model tied the most expensive model in the pack, task-for-task. The
difference was never the model's raw brain — it was the **harness**: precise
prompts, gated acceptance, context packs, adversary review. Free models are not
worse; they are more sensitive to the harness built around them, which means the
harness is the moat, and the moat compounds.

## The three fronts

The project runs three money-making fronts. Each has its own folder, its own
swarm config, and its own quadrant on the dashboard.

### Front 1 — OpenRouter-Studio (video game studio)
- **Where:** `/Users/devonodell/Desktop/internetmoney/video-games/openRouter-Studio`
- **Swarm config:** `swarm/configs/openRouter-Studio-ab0a4a.json`
- **What it is:** a swarm-powered game development studio, ~15 games and a
  website, testing games live with public access, selling private versions and
  servers at $1 each (for now).
- **Success metric:** shipped, playable games at near-zero marginal cost.

### Front 2 — desktop.influencers (image-generating influencer suite)
- **Where:** `/Users/devonodell/Desktop/influencers`. (A duplicate `influencer-studio/`
  build had drifted into this repo; it was removed — this is the one real target.)
- **Swarm config:** `swarm/configs/influencers.json`
- **What it is:** AI Girlfriend experiences sold on Fanvue and Instagram, with
  the sole intent of money extraction. Three personas, all fictional adults:
  Sora Park (26), Amara Brooks (28), Clara Hayes (27).
- **Success metric:** LTV = `monthly_price × avg_months_retained + PPV/tips`.
  The lever is retention, and retention is moved by response latency and voice
  consistency — the swarm earns its keep there.

### Front 3 — the swarm itself (the mothership)
- **Where:** this folder (`/Users/devonodell/Desktop/OpenRouterSwarm`)
- **What it is:** the harness that powers both fronts. It is pointed at three
  things and three things only:

## The three axes

EVERYTHING in this folder optimizes exactly three numbers. Nothing else.

1. **QUICKNESS** — wall-clock time from task in → accepted work out.
   Measured by SABER per-task seconds, and per-request latency learned in
   `learn.json` (pacing.py EWMA).
2. **ACCURACY** — first-try acceptance rate, per model and per persona.
   Measured by the leaderboard (`report_leaderboard.py`) and SABER pass rate.
3. **SPEND** — dollars per accepted task (free vs paid), enforced per repo by
   `daily_cap` / `monthly_usd` / the editor wallet (`bridge.py wallet`).

The daily discipline: run SABER (below), look at the three-axis trend, and make
exactly one change to the harness that moves one of the axes. Then re-run SABER
and prove the delta. Ship nothing on vibes.

## SABER — the measurement loop

SABER (and the leaderboard it feeds) now lives in `swarm-measurement-lab`, a sibling
checkout — split out because it's tuning infrastructure, not something the swarm itself needs
at runtime. The entry point there is unchanged:

```sh
.venv/bin/python swarm/saber.py --models dots-studio/dots-3-note-preview:free,openrouter:openai/gpt-5-mini
.venv/bin/python swarm/saber.py --trend      # show the last N runs from sqlite history
```

It runs the same task battery through the same flint harness as the swarm
(`flint.py --yolo` in a temp worktree), so the only variable is the model. It
captures:

- **accuracy** — objective verifier pass/fail per task (the same shape as the
  swarm's own gate)
- **quickness** — wall seconds per task, including provider 429/pacing
  backoff, because that is the latency the user actually feels
- **spend** — summed FLINT_CHARGE_FILE charges per run

Every run is appended to a time series, so "did the harness get faster/more accurate/cheaper"
is a trend line, not a feeling. Run it weekly against the current model pool, in that other
checkout, and bring a finding back here as a focused change — not the tool itself.

## Ground rules

- Never edit the fronts' checkouts from here; the swarm works in worktrees.
- The swarm folder optimizes the three axes; the front folders ship product.
- A change to the harness earns its keep only when SABER or the leaderboard
  proves the delta on one of the three axes.
- Free is the primary pool (`dots-3` stays in it and rests when rate-limited);
  paid models exist only as a hedge after the shared free allowance runs out.