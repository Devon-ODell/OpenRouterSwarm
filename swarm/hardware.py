#!/usr/bin/env python3
"""Hardware-aware local-model recommendation (the `/doctor` equivalent, Phase 2).

Cursor's dropdown and albatross's `/doctor recommend` both answer the same question:
given the machine I have, which local server and which model will actually fit?
This module reads the machine it runs on — CPU count, RAM, Apple Silicon total
memory — and says which class of model a server can run without thrashing.

It never runs `sysctl` or `ps` against a remote box; it only reads this machine,
which is exactly the scope `/doctor` cares about. Everything here is conservative:
where a value cannot be read (Linux /proc absent, say) the recommendation falls
back to the smallest tier rather than guessing upward.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

# GB a server + OS realistically need before the model even loads.
OS_OVERHEAD_GB = 4.0
# A model's weights are roughly its parameter count in billion bytes at 8-bit,
# plus a context window, plus the server's own working set.
BYTES_PER_PARAM = 1.0  # ≈ one byte per parameter at 8-bit quantization


def _ram_gb():
    """Total physical RAM in GB, or None when it cannot be read."""
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return float(out) / (1024 ** 3) if out.isdigit() else None
        if os.path.exists("/proc/meminfo"):
            for line in open("/proc/meminfo", errors="replace"):
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _cpu_count():
    try:
        return os.cpu_count() or None
    except Exception:
        return None


def _apple_silicon():
    """True on Apple Silicon (arm64 macOS); the unified-memory tier applies."""
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def spec():
    """The machine facts the recommender reasons over."""
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu": _cpu_count(),
        "ram_gb": _ram_gb(),
        "apple_silicon": _apple_silicon(),
    }


# Model tiers, biggest-first, with the minimum RAM (GB) and a friendly label.
TIERS = [
    # name, min_ram_gb, label
    ("huge", 64.0, "70B+ class — needs a big workstation or M-series Max/Ultra"),
    ("large", 32.0, "30B–34B class — M-series Pro/Max or a 32 GB+ workstation"),
    ("medium", 16.0, "7B–14B class — comfortable on 16 GB unified memory"),
    ("small", 8.0, "3B–7B class — fits on an 8 GB Mac or a modest laptop"),
    ("tiny", 4.0, "1B–3B class — the safe pick when RAM is tight"),
]


def recommend(ram_gb=None):
    """(tier, ram_gb, is_apple_silicon, detail) fitting this machine.

    The recommendation is a *ceiling*: the biggest tier that leaves the OS a
    working margin, or `tiny` when the machine cannot be measured at all.
    """
    ram = ram_gb if ram_gb is not None else _ram_gb()
    apple = _apple_silicon()
    if ram is None:
        return "tiny", None, apple, TIERS[-1][2]
    usable = ram - OS_OVERHEAD_GB
    for name, need, label in TIERS:
        if usable >= need:
            return name, round(ram, 1), apple, label
    return "tiny", round(ram, 1), apple, TIERS[-1][2]


def server_choice(ram_gb=None):
    """Which local server to point the picker at, from what this machine has.

    Apple Silicon prefers `mlx` (the MLX server is optimized for the unified
    memory); anything else lands on Ollama, the most turnkey choice.
    """
    _, _, apple, _ = recommend(ram_gb)
    return "mlx" if apple else "ollama"


def model_hint(provider, ram_gb=None):
    """A concrete model id to offer first for a local provider, or None.

    Only providers whose model names we can guess usefully are covered; for the
    rest the picker falls back to the server's own /models list.
    """
    tier, _, apple, _ = recommend(ram_gb)
    if provider == "ollama":
        return {
            "huge": "qwen2.5-coder:32b", "large": "qwen2.5-coder:14b",
            "medium": "qwen2.5-coder:7b", "small": "qwen2.5-coder:3b",
            "tiny": "qwen2.5:1.5b",
        }.get(tier)
    if provider == "mlx" and apple:
        return {
            "huge": "qwen2.5-coder:32b", "large": "qwen2.5-coder:14b",
            "medium": "qwen2.5-coder:7b", "small": "qwen2.5-coder:3b",
            "tiny": "qwen2.5:1.5b",
        }.get(tier)
    if provider in ("lm-studio", "llamacpp"):
        return None  # the server's own list is the honest answer
    return None


def doctor():
    """The `/doctor` payload: machine facts plus a recommendation."""
    h = spec()
    tier, ram, apple, label = recommend(h["ram_gb"])
    server = server_choice(h["ram_gb"])
    hint = model_hint(server, h["ram_gb"])
    return {
        "hardware": h,
        "tier": tier,
        "detail": label,
        "recommended_server": server,
        "recommended_model": hint,
        "apple_silicon": apple,
    }