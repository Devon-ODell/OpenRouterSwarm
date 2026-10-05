# YouTube Script — "We Built a Free ChatGPT Killer (3 Open-Source Models, Majority Vote)"

**Format:** ~4–5 min explainer / build video
**Voice:** edge-tts `en-US-GuyNeural` (warm, conversational, slight storyteller energy)
**Pacing:** hook < 15s, lesson ~2min, deploy ~1.5min, ending ~20s
**Scenes:** S1…S8 — each maps to one animation shot in `animation_schema.md`.

---

## S1 — HOOK (0:00–0:18)  [THE PRICE-HIKE BOMBSHELL]

*Narration (energetic, a little indignant):*

> "ChatGPT just announced they're **halving your token allowance** — and
> **raising prices**. Again. Meanwhile you're sitting here, paying monthly, and
> wondering: *isn't there another way?*
> What if I told you there's a system you can run **entirely for free**, that
> does the same job — and in some tasks, even better — using three tiny
> open-source models that vote like a jury?
> I built it. And today I'm going to show you how to deploy it yourself."

*On-screen: bold headline "TOKENS CUT. PRICES UP." then cut to code/terminal.*

---

## S2 — THE ARMY SAYING (0:18–0:45)  [THE THESIS]

*Narration (slower, storyteller mode):*

> "There's an old Army saying: *how many watches do you need to tell the time
> anywhere in the world? Three watches — because one is probably wrong, but
> without the third, you won't know which is right.*
> That's the whole idea. One model hallucinates. Two models *agree* — you can
> trust it. And the third watch? It's what tells you *which* one is lying."

*On-screen: three watches/animated agents, two agree, one glows red.*

---

## S3 — THE PROBLEM: RISING TOKEN COSTS (0:45–1:30)  [THE STAKES]

*Narration:*

> "Here's the thing. Every year, AI gets 'better' — and every year, the meter
> runs faster. Frontier models cost real money per token, and that cost is
> *going up, not down*. For a student, a hobbyist, even a small startup — that
> monthly bill is brutal.
> But look closer. The *capability* you actually need — writing code, fixing
> typos, translating text — a lot of that can be done by **small, free,
> open-source models**. Models that cost you exactly **zero dollars**."

*On-screen: meter climbing / dollar signs / a wall of rising-stock bars, then a "$0.00" stamp.*

---

## S4 — THE LESSON: THREE HEADS BEAT ONE (1:30–2:20)  [TEACH THE "FIGHT BACK" MECHANISM]

*Narration (teaching mode, confident):*

> "So here's the lesson — *how to fight back*.
> You take **three small free models**, all from different families so their
> mistakes don't line up. You give each one the *same task*, in its own sandbox —
> no peeking at each other. Then you run a **verifier** — a real check, not an
> opinion — and you **majority-vote** the results.
> Two agree? Ship it. One disagrees? The majority wins, and the odd one out is
> exactly what the Army saying warned us about: the watch that's wrong.
> That's it. That's the whole trick. No expensive frontier model required."

*On-screen: 3 agents, 3 sandboxes, verifier checkmark, 2-vs-1 vote animation.*

---

## S5 — THE RESULTS: WHAT WE MEASURED (2:20–3:10)  [RECEIPTS]

*Narration (proud, concrete):*

> "And it's not just a theory — I benchmarked it against **GPT-5.5**.
> On 24 real tasks — 14 coding, 10 translations into Spanish, French, Japanese,
> Arabic, you name it — the three-model consensus scored **22 out of 24**.
> GPT-5.5? *Also* 22 out of 24. Same score. But here's the kicker:
> the free panel cost me **zero dollars**. GPT-5.5: seventy-five *cents* — for
> the same result.
> Translation was even cleaner: **10 out of 10, free.** My best single free
> model actually *beat* GPT-5.5. The only trade-off? It's slower — about ten
> times. But you're not paying for speed, you're paying for answers. And these
> answers are free."

*On-screen: side-by-side bar chart — accuracy matches, cost $0.00 vs $0.75, "10x slower" note.*

---

## S6 — HOW TO DEPLOY IT (3:10–4:00)  [THE WALKTHROUGH]

*Narration (step-by-step, encouraging):*

> "Ready to run it yourself? Here's the deploy, start to finish.
> **Step one** — clone the repo and set up Python:
> `git clone <repo> && cd OpenRouterSwarm && python -m venv .venv`
> **Step two** — get a free OpenRouter API key and drop it in `.env`:
> `OPENROUTER_API_KEY=sk-or-...`
> **Step three** — run the consensus benchmark:
> `.venv/bin/python consensus/consensus_runner.py --suite translation --frontier-raw`
> That fires three free models at every task, votes, and logs everything to
> `runs.jsonl` — your own reproducible receipts.
> **Step four** — see the scoreboard:
> `.venv/bin/python consensus/report.py`
> Free. Local. Yours. No subscription."

*On-screen: type the commands in real time, terminal-style; cut to the jsonl log table.*

---

## S7 — WHY THIS MATTERS (4:00–4:25)  [THE BIGGER PICTURE]

*Narration (grounded, sincere):*

> "This isn't about hating ChatGPT. The frontier models are amazing. It's about
> *not being trapped* by the meter. When the price goes up, you should have a
> lever to pull. Three free models, a majority vote, and an honest verifier —
> that lever is real, it's open source, and it's sitting on your laptop right
> now."

*On-screen: a lever graphic / a door opening; the words "THE LEVER IS REAL".*

---

## S8 — ENDING (4:25–4:50)  [FINAL THOUGHTS + THE JOKE]

*Narration (warm, then the beat):*

> "So the next time your token allowance gets cut, remember the Army saying.
> One watch is probably wrong. Two, you can trust. And three — that's how you
> know which one's right.
> **Final Thoughts.**

*(screen goes blank — hold for three seconds — audio silence too)*

> **"Thank You, China."** *(dry, wry, then a beat)*
>
> *(lighter)* "And thanks to Qwen and the open-source community — the models that
> made this free are real, and they're excellent. Like and subscribe; I'll see
> you in the next one."

*On-screen: after blank, a wry card "Thank You, China 🇨🇳 — seriously though, thanks Qwen"; end-card with subscribe button.*

---

## Production notes

- Total narration ≈ 4 min 45 s; with beats/pauses the video lands ~5 min.
- Tone: indignant → conspiratorial-fun → teacher → proud → sincere → wry.
- The 3-second blank before "Thank You China" is the comedic beat — keep the
  silence absolute (no background music) for maximum deadpan.
- All numbers come from `consensus/runs/runs.jsonl` (24 tasks, 22/24 each,
  $0.00 vs $0.75, translation 10/10).