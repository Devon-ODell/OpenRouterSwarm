"""Text-to-speech generator for the YouTube script.

Uses edge-tts (neural Microsoft voices, free, no key) to render each scene's
narration to audio/scene_S<n>.mp3 and a concatenated audio/full.mp3.

Usage:
    .venv/bin/python video/tts_make.py            # render all scenes
    .venv/bin/python video/tts_make.py --scene S3 # one scene
    .venv/bin/python video/tts_make.py --voice en-GB-SoniaNeural

The narration text is defined inline below (mirrors video/SCRIPT.md). Per-scene
duration is printed so the editor can sync the animation schema shot-by-shot.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"

VOICE = "en-GB-RyanNeural"  # deep British, dry-documentary storyteller
RATE = "+0%"                # edge-tts rate; "+8%" feels a touch livelier
VOLUME = "+0%"

# edge-tts hits Microsoft's endpoint; firing requests back-to-back gets rate-
# limited (the connection stalls). Keep a small gap + retry with backoff.
SCENE_GAP_S = 1.5
MAX_RETRIES = 3

# scene narration — mirrors video/SCRIPT.md, one entry per scene
SCENES = {
    "S1": (
        "ChatGPT just announced they're halving your token allowance — and raising prices. "
        "Again. Meanwhile you're sitting here, paying monthly, and wondering: isn't there another way? "
        "What if I told you there's a system you can run entirely for free, that does the same job — "
        "and in some tasks, even better — using three tiny open-source models that vote like a jury? "
        "I built it. And today I'm going to show you how to deploy it yourself."
    ),
    "S2": (
        "There's an old Army saying: how many watches do you need to tell the time anywhere in the world? "
        "Three watches — because one is probably wrong, but without the third, you won't know which is right. "
        "That's the whole idea. One model hallucinates. Two models agree — you can trust it. "
        "And the third watch? It's what tells you which one is lying."
    ),
    "S3": (
        "Here's the thing. Every year, AI gets better — and every year, the meter runs faster. "
        "Frontier models cost real money per token, and that cost is going up, not down. "
        "For a student, a hobbyist, even a small startup — that monthly bill is brutal. "
        "But look closer. The capability you actually need — writing code, fixing typos, translating text — "
        "a lot of that can be done by small, free, open-source models. Models that cost you exactly zero dollars."
    ),
    "S4": (
        "So here's the lesson — how to fight back. "
        "You take three small free models, all from different families so their mistakes don't line up. "
        "You give each one the same task, in its own sandbox — no peeking at each other. "
        "Then you run a verifier — a real check, not an opinion — and you majority-vote the results. "
        "Two agree? Ship it. One disagrees? The majority wins, and the odd one out is exactly what "
        "the Army saying warned us about: the watch that's wrong. That's it. That's the whole trick. "
        "No expensive frontier model required."
    ),
    "S5": (
        "And it's not just theory — I benchmarked it against GPT-5.5, head to head. "
        "Twenty-four real tasks: fourteen coding challenges, ten translations — "
        "Japanese, Arabic, Hindi, Russian, you name it. "
        "And every one is checked by a deterministic verifier, not an AI judge. "
        "The code has to compile and pass real tests. "
        "The translations have to match the ground truth, character for character. "
        "The panel tied GPT-5.5 at twenty-two out of twenty-four — and cost me exactly zero dollars, "
        "against roughly seventy-five cents for the same score from the frontier. "
        "But the individual performances are the interesting part. "
        "North-mini, the smallest model on the panel, went thirteen for fourteen on the coding suite — "
        "one up on GPT-5.5, which missed two. My favourite moment: a task that read a JSON file of users "
        "and had to return the active adults, sorted. North-mini nailed it in seven seconds — returned Alice. "
        "GPT-5.5 failed that same task entirely, spending four cents trying. "
        "Translations were the cleanest sweep: ten for ten, free. And not trivial ones. "
        "A Japanese see you tomorrow. An Arabic peace be upon you. A Russian good luck with your exam. "
        "On three of those — Japanese, Italian, Russian — one of the three free models actually failed. "
        "Two agreed, the majority won, and the answer shipped. "
        "The vote rescued exactly the tasks where a single free model stumbled. "
        "That's the Army saying, working live. "
        "The only trade-off is speed — about ten times slower than GPT-5.5. "
        "But you're not paying for speed, you're paying for answers. And these answers are free."
    ),
    "S6": (
        "Everything I ran is open source, sitting in the repo. "
        "Clone it, drop in a free API key, and one command reproduces every number you just saw. "
        "Full setup and the exact commands are in the description below. "
        "Free. Local. Yours. No subscription."
    ),
    "S7": (
        "This isn't about hating ChatGPT. The frontier models are amazing. "
        "It's about not being trapped by the meter. When the price goes up, you should have a lever to pull. "
        "Three free models, a majority vote, and an honest verifier — "
        "that lever is real, it's open source, and it's sitting on your laptop right now."
    ),
    "S8": (
        "So the next time your token allowance gets cut, remember the Army saying. "
        "One watch is probably wrong. Two, you can trust. And three — that's how you know which one's right. "
        "Final Thoughts."
        # NOTE: 3s black + silent beat goes here in the edit (no audio),
        # then resume with the wry beat:
    ),
    "S8b": (  # the comedic punchline, rendered as its own clip for the post-blank timing
        "Thank You, China."
    ),
    "S8c": (
        "And thanks to Qwen and the open-source community — the models that made this free are real, "
        "and they're excellent. Like and subscribe; I'll see you in the next one."
    ),
}


def _fn(scene: str) -> Path:
    return AUDIO / f"scene_{scene}.mp3"


async def _render(scene: str, text: str, voice: str) -> float:
    t0 = time.time()
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=RATE, volume=VOLUME)
            # edge-tts can HANG (not error) when Microsoft throttles; a hard
            # timeout converts the stall into a retryable failure.
            await asyncio.wait_for(communicate.save(str(_fn(scene))), timeout=60)
            return time.time() - t0
        except Exception as e:  # network/rate-limit hiccup — wait, retry
            last = e
            wait = SCENE_GAP_S * (2 ** attempt)
            print(f"    retry {attempt}/{MAX_RETRIES} in {wait:.0f}s for {scene}: "
                  f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            await asyncio.sleep(wait)
    raise RuntimeError(f"{scene} failed after {MAX_RETRIES} attempts: {last}") from last


async def _concat(out: Path, scenes_in_order):
    """Concatenate rendered scene mp3s with ffmpeg (keeps timing aligned)."""
    import subprocess
    parts = [str(_fn(s)) for s in scenes_in_order]
    listfile = AUDIO / ".parts.txt"
    listfile.write_text("\n".join(f"file '{p}'" for p in parts) + "\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c:a", "libmp3lame", "-q:a", "2", str(out)],
        check=True, capture_output=True)


def duration_secs(path: Path) -> float:
    """Approx duration via ffprobe (ffmpeg ships it)."""
    import json
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


async def main_async(args):
    AUDIO.mkdir(exist_ok=True)
    voice = args.voice
    scenes = [args.scene] if args.scene else list(SCENES)
    print(f"voice: {voice}")
    for s in scenes:
        text = SCENES.get(s)
        if not text:
            print(f"!! unknown scene {s}", file=sys.stderr)
            continue
        dur = await _render(s, text, voice)
        secs = duration_secs(_fn(s))
        print(f"  {s}: {secs:.1f}s  (render {dur:.1f}s)  -> {_fn(s).relative_to(ROOT)}")
    # full track: S1..S7, S8 (up to 'Final Thoughts.'), [3s blank], S8b, S8c
    order = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S8b", "S8c"]
    if all(_fn(s).exists() for s in order):
        out = AUDIO / "full.mp3"
        await _concat(out, order)
        print(f"full track: {duration_secs(out):.1f}s  -> {out.relative_to(ROOT)}")
        print("Pacing note: insert a 3s black+silent gap after scene_S8.mp3 "
              "(before 'Thank You, China.') per the animation schema.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default=None, help="render one scene (e.g. S3)")
    ap.add_argument("--voice", default=VOICE)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()