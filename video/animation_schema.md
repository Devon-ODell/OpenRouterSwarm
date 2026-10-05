# Animation Schema — "We Built a Free ChatGPT Killer"

Maps every narration scene (S1–S8 from `SCRIPT.md`) to concrete visuals.
Format is tool-agnostic (works with After Effects, Premiere, DaVinci, HTML/canvas,
Manim, or a talking-head + B-roll edit). Each scene has: **shot**, **on-screen
text**, **motion**, and **audio cue** so the video assembles fast.

---

## S1 — HOOK: TOKENS CUT. PRICES UP. (0:00–0:18)

| field | value |
|---|---|
| shot | Full-screen kinetic typography, hard cuts, red/black palette |
| text | Big: **"TOKENS CUT."** → **"PRICES UP."** → **"AGAIN."** (each slams in on a beat) |
| motion | Punch-in scale; red alert flash; then a quick 2-frame cut to a terminal window |
| b-roll | Faint ChatGPT logo with a red slash, dollar-sign meter climbing |
| audio | Narration; a low "whoosh" on each headline; no music in the first 5s |
| end | Cut to: dark terminal, cursor blinking |

## S2 — THE ARMY SAYING: THREE WATCHES (0:18–0:45)

| field | value |
|---|---|
| shot | Flat 2D icons on a dark gradient; smooth camera pan left→right |
| text | Quote appears word-by-word: *"How many watches… Three. Because one is probably wrong…"* |
| motion | Three watch icons slide in; watch #1 ticks erratically (red glow), #2+#3 tick in sync (green glow) |
| audio | Storyteller tone; gentle clock-tick sound layer |
| end | The three watches morph into three smiling robot heads (the panel) |

## S3 — THE PROBLEM: RISING TOKEN COSTS (0:45–1:30)

| field | value |
|---|---|
| shot | Animated line chart going up, dollar signs, then a hard "stamp" |
| text | "$ per token ↑" then big green **"$0.00"** stamp |
| motion | Bar chart climbs; camera zooms out to show a wall of rising bars; a green "$0.00" stamp slams onto screen with a printer sound |
| b-roll | Person wincing at phone bill; small startup desk |
| audio | Tense low drone; the stamp is a satisfying thunk |
| end | Freeze on "$0.00" — cut to black |

## S4 — THE LESSON: THREE HEADS BEAT ONE (1:30–2:20)

| field | value |
|---|---|
| shot | Top-down sandbox diagram; 3 agents in 3 boxes, connecting lines |
| text | "3 models, 3 sandboxes, 1 verifier — majority vote" |
| motion | Same prompt flows to 3 agents (different colored paths); each produces a file; a unified "verifier" checkmark scans all three; 2 light up green, 1 red; a speech bubble pops: "2 agree → ship it" |
| audio | Confident teacher tone; soft "tick" as each agent finishes, a "ding" on the verifier |
| end | The 3 sandboxes collapse into one green checkmark |

## S5 — THE RESULTS: RECEIPTS (2:20–3:10)

| field | value |
|---|---|
| shot | Split-screen comparison bars (you vs GPT-5.5) |
| text | "24 tasks: 22/22 vs 22/22" then **"$0.00 vs $0.75"**, then "translation 10/10 FREE" |
| motion | Two bars rise equally; the cost bars animate — yours drops to $0 flatline, GPT's climbs to $0.75; a "10× slower" footnote pops with a shrug emoji |
| audio | Proud, concrete; a "cha-ching" on $0.00? No — a soft "free" chime is classier |
| end | Zoom into the $0.00 bar |

## S6 — HOW TO DEPLOY (3:10–4:00)

| field | value |
|---|---|
| shot | Clean terminal mock-up (not real screen recording — cleaner), typing in real time |
| text | The 4 commands, typed one per line with a blinking cursor |
| motion | Each line types out; after `report.py`, a table fades in (per-task pass/fail + cost) |
| audio | Keyboard clacks synced to typing; encouraging voiceover |
| end | Terminal shrinks to a window on a desktop — "it's just… running" |

## S7 — WHY THIS MATTERS (4:00–4:25)

| field | value |
|---|---|
| shot | Metaphor: a lever next to a token meter |
| text | "THE LEVER IS REAL" |
| motion | Slow pull of the lever; the meter drops from $0.75 → $0.00; a door behind swings open |
| audio | Sincere; a satisfying lever "clunk" |
| end | Door light floods the screen (white-out transition to S8) |

## S8 — ENDING: FINAL THOUGHTS + THE JOKE (4:25–4:50)

| field | value |
|---|---|
| shot | Warm medium close (talking head) or calm end-screen graphics |
| text | "Final Thoughts:" appears… then **screen goes fully BLACK (hold 3 s, no audio)** |
| motion | After the 3-s blank, a wry card pops: **"Thank You, China 🇨🇳"** followed by a smaller line *"seriously though — thanks Qwen & the open-source community"* |
| audio | Dead silence during the blank (no music!) — that's the joke; then a dry "Thank You, China." and a lighter post-line; end-card "Like & Subscribe" with a soft outro sting |
| end | Subscribe button + "next video" card |

---

## Global style (keep consistent)

- **Palette:** dark navy/black bg, cyan accent, white text; red ONLY for "wrong/no"
  states, green ONLY for "pass/agree".
- **Type:** bold sans (Inter / Montserrat); headlines ≥ 96px, body 48–64px.
- **Motion:** 150–250ms ease-out for entrances; nothing wiggly except the
  "wrong watch" in S2.
- **Audio:** narration (edge-tts `en-US-GuyNeural`) as the spine; music ducked
  -18dB under voice; the S8 blank is the one place with NO audio at all.
- **Text on screen ≤ 8 words a time** — the narration carries the detail.

## Assembly order (for the editor)

1. Drop narration clips (from `audio/scene_S*.mp3`) on the timeline.
2. Match each scene's visuals to its narration clip.
3. Insert the 3-second **black + silent** beat at the S8 mark (before "Thank You, China").
4. Adjust S8's wry card to land exactly on "Thank You, China." — a beat after the blank.
5. Add global music under everything except S1's first 5s and the S8 blank.