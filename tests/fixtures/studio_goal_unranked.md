# Goal

## Current owner requirements (2026-09-26)

- Next work is designed in [docs/roadmap/README.md](docs/roadmap/README.md), with
  ordered work packets in [docs/roadmap/tasks.json](docs/roadmap/tasks.json):
  tutorials for all fourteen games, meaningful progression, validated procedural
  variety, late-game mastery and original pixel art. **The owner started the
  implementation phase on 2026-09-27**, so that roadmap is now the work. Its
  ledger is [docs/roadmap/EXECUTION.md](docs/roadmap/EXECUTION.md); BASE is
  accepted. Work the packet you were given — read its section of the roadmap and
  the contracts it names, and do not invent another roadmap or widen the packet.
  The roadmap supersedes the legacy dispatch order below, not accepted game
  behavior.
- Shard Stack uses only the seven standard four-cell pieces. Preserve the accepted
  gameplay thumbnails and full-frame card display.
- Coordinate all Goblins! edits with its existing owner; do not rebuild or replace
  its accepted gin rules, opponents, trinkets or progression.
- Star Catcher and Reversi are deliberately removed. Do not restore them.
- Keep Chess and Goblins! (renamed from Go Gin, Goblins!, and rewritten as real gin rummy) in the playable arcade.
- Preserve Cinder Hop's pixel sprites and retro handheld presentation.
- Shard Stack control reliability and collision correctness take priority over effects.
- Port 8000 serves tested committed snapshots; an update button sits beside Report a bug.
- Describe progress using accepted commits and test results; routing to a paid model does not prove a charge.

Build **openRouter-Studio**: a looping, fully automated game studio that
invents, builds, tests, packages and (eventually) publishes small games on its
own, run by the flint swarm on free OpenRouter models.

Reference games (read-only, never edit; use these absolute paths, `../` does
not reach them from a worktree):
`/Users/devonodell/Desktop/internetmoney/video-games/{claude-of-duty-main,icebowl,paintball}`.
What they teach is in `docs/reference/`.

## Status (2026-09-25)

Built and tested: every pipeline stage (concept → spec → build → gate →
playtest → package → publish as a dry run) in `studio/pipeline.py`; the headless
playtest `studio/harness/sim.js`; the template `studio/templates/html5/`;
`python3 -m studio.new`; `python3 -m studio.loop --once` with
`ledger/runs.jsonl`; and two games in `games/`, a catcher (the template) and
`wisp-warden`, a pocket roguelite; `python3 -m studio.arcade` serves them all with a
home screen and bug reports (`reports/bugs.jsonl`). **Read `docs/GAME_CONTRACT.md` before any
game work.**

## Legacy backlog (reference, not the next dispatch order)

1. **The four deep games in `docs/briefs/`** (falling blocks, match-3, platformer,
   kart racer): one milestone per task, in the order of `docs/briefs/tasks.json`. Each
   milestone must PASS `python3 -m studio.pipeline games/<slug>` on its own. Read
   `docs/briefs/README.md` and the MIT CMS.608 game design skill it names first.
   Genre yes, clone no: obey each brief's Do not copy list.
2. **Better existing games:** smarter bots (a bot worse than a casual human is
   a bug), sharper targets, more juice in `render`. Bump `version` when a
   released game changes.
3. **Small tested studio features:** a tuning loop over a game's `CONFIG` block
   against its targets (icebowl's pattern); more bot skill tiers; a content
   word check in `build`; a WebAudio sound hook in `shell.js`; an optional
   real-browser smoke test that skips when no Chrome is installed.
4. Roblox (Rojo + Lune) only once html5 games flow.

## Rules

- A game task changes only its declared game paths and focused test files. The
  roadmap's presentation packets may also update that game's thumbnail; only the
  shared-runtime owner synchronizes shell copies. A studio task adds no games.
  Keep diffs small; never have two workers edit the same game.
- **Deterministic gates, not model opinions.** A game passes only when build,
  gate and playtest pass and every manifest target is met. Never loosen a
  target to pass: fix the game or its bot. Model reviews are advisory.
- Standard library Python only for the studio (tests run with
  `python3 -m unittest discover -s tests -t . -q`). Games are plain browser
  JavaScript; any Node check must skip when `node` is absent.
- Tests write only to temporary folders, never inside the repo.
- Never remove or weaken an existing test.
- `studio/publish.py` is dry-run by default and in every test. A real upload
  (itch.io `butler push`) runs only when `BUTLER_API_KEY` is set in the
  environment of a separate, human-scheduled job. The swarm never reads,
  writes or asks for credentials.
- Keep every game original: no third-party characters, names, art or audio.
