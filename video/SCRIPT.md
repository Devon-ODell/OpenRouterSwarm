# YouTube Script — "We Built a Free ChatGPT Killer (3 Open-Source Models, Majority Vote)"

**Format:** ~4–5 min explainer / build video
**Voice:** edge-tts `en-GB-RyanNeural` (deep British, dry documentary storyteller)
**Pacing:** hook < 30s, lesson ~1.5min, results ~2min, deploy ~25s, ending ~30s
**Scenes:** S1…S8 — each maps to one animation shot in `animation_schema.md`.

---

## S1 — HOOK (0:00–0:28)  [THE PRICE-HIKE BOMBSHELL]

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

## S2 — THE ARMY SAYING (0:28–0:52)  [THE THESIS]

*Narration (slower, storyteller mode):*

> "There's an old Army saying: *how many watches do you need to tell the time
> anywhere in the world? Three watches — because one is probably wrong, but
> without the third, you won't know which is right.*
> That's the whole idea. One model hallucinates. Two models *agree* — you can
> trust it. And the third watch? It's what tells you *which* one is lying."

*On-screen: three watches/animated agents, two agree, one glows red.*

---

## S3 — THE PROBLEM: RISING TOKEN COSTS (0:52–1:26)  [THE STAKES]

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

## S4 — THE LESSON: THREE HEADS BEAT ONE (1:26–2:04)  [TEACH THE "FIGHT BACK" MECHANISM]

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

## S5 — THE RESULTS: WHAT WE MEASURED (2:04–4:00)  [RECEIPTS — THE ACTUAL TESTS]

*Narration (proud, concrete — each claim is a real logged run):*

> "And it's not just theory — I benchmarked it against **GPT-5.5**, head to
> head. Twenty-four real tasks: fourteen coding challenges, ten translations —
> Japanese, Arabic, Hindi, Russian, you name it.
> And every one is checked by a *deterministic* verifier, **not an AI judge**.
> The code has to compile and pass real tests. The translations have to match
> the ground truth, character for character.
>
> The panel tied GPT-5.5 at **twenty-two out of twenty-four** — and cost me
> exactly **zero dollars**, against roughly **seventy-five cents** for the same
> score from the frontier. But the individual performances are the interesting
> part.
>
> North-mini, the smallest model on the panel, went **thirteen for fourteen**
> on the coding suite — *one up* on GPT-5.5, which missed two. My favourite
> moment: a task that read a JSON file of users and had to return the **active
> adults, sorted**. North-mini nailed it in **seven seconds** — returned
> Alice. **GPT-5.5 failed that same task entirely**, spending four cents
> trying.
>
> Translations were the cleanest sweep: **ten for ten, free**. And not trivial
> ones. A Japanese *see you tomorrow*. An Arabic *peace be upon you*. A Russian
> *good luck with your exam*. On three of those — Japanese, Italian, Russian —
> one of the three free models actually *failed*. Two agreed, the majority won,
> and the answer shipped. The vote rescued exactly the tasks where a single
> free model stumbled. That's the Army saying, working live.
>
> The only trade-off is speed — about **ten times** slower than GPT-5.5. But
> you're not paying for speed, you're paying for answers. And these answers
> are free."

*On-screen: side-by-side bar chart (22/24 vs 22/24, $0.00 vs $0.75); zoom into the JSON users task — north-mini ✓ in 7s vs GPT-5.5 ✗; then flash translation cards ("See you tomorrow." → また明日。 / "Peace be upon you." → السلام عليكم / "Good luck with your exam." → Удачи на экзамене.) with a "2 agree → shipped" tick on the rescued ones.*

---

## S6 — HOW TO RUN IT (4:00–4:21)  [THE WALKTHROUGH, LEAN]

*Narration (quick, confident):*

> "Everything I ran is open source, sitting in the repo. Clone it, drop in a
> free API key, and **one command reproduces every number you just saw**.
> Full setup and the exact commands are in the description below.
> Free. Local. Yours. No subscription."

*On-screen: repo card + one command line, then a soft cut to the runs table. Full instructions live in the video description.*

---

## S7 — WHY THIS MATTERS (4:21–4:42)  [THE BIGGER PICTURE]

*Narration (grounded, sincere):*

> "This isn't about hating ChatGPT. The frontier models are amazing. It's about
> *not being trapped* by the meter. When the price goes up, you should have a
> lever to pull. Three free models, a majority vote, and an honest verifier —
> that lever is real, it's open source, and it's sitting on your laptop right
> now."

*On-screen: a lever graphic / a door opening; the words "THE LEVER IS REAL".*

---

## S8 — ENDING (4:42–5:09)  [FINAL THOUGHTS + THE JOKE]

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

- Total narration ≈ 5 min (309.5s rendered); with the 3s beat the video lands ~5m12s.
- Tone: indignant → conspiratorial-fun → teacher → proud → sincere → wry.
- The 3-second blank before "Thank You China" is the comedic beat — keep the
  silence absolute (no background music) for maximum deadpan.
- All numbers come from `consensus/runs/runs.jsonl` (24 tasks, 22/24 each,
  $0.00 vs $0.747, translation 10/10, north-mini 13/14 coding vs GPT-5.5 12/14,
  json-filter: north-mini pass in 7s vs GPT-5.5 fail, vote rescued
  tr-en-ja/it/ru). The deterministic verifiers are real — code compiles + runs,
  translations are exact character matches.