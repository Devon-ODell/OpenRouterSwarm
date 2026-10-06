# Video Production Kit — "We Built a Free ChatGPT Killer"

Everything you need to assemble the YouTube video. The **narration audio is
already generated** — you just splice scenes and add visuals.

## What's here

| file | what |
|---|---|
| `SCRIPT.md` | Full narration script (S1–S8), with tone + pacing + the 3s-blank ending |
| `animation_schema.md` | Scene-by-scene visual directives (shot / text / motion / audio) |
| `tts_make.py` | Text-to-speech generator (edge-tts, free, no key) |
| `audio/scene_S*.mp3` | Per-scene narration clips (already rendered, `en-GB-RyanNeural`) |
| `audio/full.mp3` | The full concatenated track (rendered, `en-GB-RyanNeural`) |

## The audio

- Voice: `en-GB-RyanNeural` (deep British, dry documentary storyteller, edge-tts neural)
- `full.mp3` = S1→S7 + S8 (ends "...which one's right. **Final Thoughts.**")
  + `S8b` ("Thank You, China." — 2.1s) + `S8c` (thanks + subscribe)
- **The 3-second black + silent beat goes in the EDIT between S8 and S8b** —
  the schema and script both mark it. No audio in that gap (that's the joke).
- **The audio lives locally in `video/audio/` and is git-ignored** — GitHub
  rejects the mp3 binaries on push (RPC 400). It's fully regenerable with
  `tts_make.py`, so nothing is lost by keeping it out of the repo.

## Regenerate audio (if you change the script)

```sh
# one scene
.venv/bin/python video/tts_make.py --scene S3
# all scenes + full track
.venv/bin/python video/tts_make.py
# different voice
.venv/bin/python video/tts_make.py --voice en-GB-SoniaNeural
```

Notes:
- edge-tts hits Microsoft's endpoint; back-to-back calls can stall silently.
  `tts_make.py` now uses a 60s per-call timeout + retry with backoff, and
  renders each scene to its own file. If a scene hangs, just re-run the single
  scene command.
- Requires `edge-tts` (in the venv) and `ffmpeg`/`ffprobe` (installed).

## Assemble the video (fast path)

1. Drop `audio/scene_S1..S8.mp3` on a timeline in order.
2. Match each scene to `animation_schema.md`'s visuals (they're labeled S1..S8).
3. After scene S8 (which ends on "Final Thoughts."), insert **3 seconds of
   black + silence**.
4. Drop `scene_S8b.mp3` ("Thank You, China.") immediately after the blank.
   Then `scene_S8c.mp3` for the sign-off.
5. Add global music ducked under voice except S1's first 5s and the S8 blank.

## The hook (S1) & the joke (S8b) — don't lose them

- S1 opens hard: "ChatGPT just announced they're halving your token allowance
  — and raising prices." → cut to the "$0.00 vs $0.75" receipts.
- S8b "Thank You, China." after a dead-frozen screen is the punchline; keep the
  silence total (no music, no riser).