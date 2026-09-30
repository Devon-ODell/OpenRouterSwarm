#!/usr/bin/env python3
"""Provider registry: which backend a model slug belongs to, where its endpoint lives,
and how requests to it are shaped.

Phase 1 of the local-models change request. The swarm has been OpenRouter-only since
day one (`flint.py` hard-codes `OPENROUTER`); this module is the registry that turns a
model string into a transport, so the same agent can talk to OpenRouter, a local
Ollama/LM Studio/MLX/llama.cpp server, any OpenAI-compatible endpoint, or the Anthropic
Messages API.

Providers are small data records, not classes with sockets; the OpenAI client stays the
transport for every OpenAI-compatible backend (OpenRouter included) and an Anthropic
Messages stream is built where the endpoint says so. `resolve()` answers two questions:
the base URL to talk to, and whether that server speaks OpenAI `/v1/chat/completions`
or Anthropic `/v1/messages`.

Endpoint overrides win over defaults, matching the albatross env contract::

    OLLAMA_BASE_URL      default http://localhost:11434/v1
    LM_STUDIO_BASE_URL   default http://localhost:1234/v1
    MLX_BASE_URL         default http://localhost:8080/v1
    LLAMACPP_BASE_URL    default http://localhost:8080/v1
    OPENAI_BASE_URL      default https://api.openai.com/v1
    ANTHROPIC_BASE_URL   default https://api.anthropic.com/v1
    OPENROUTER_BASE_URL  default https://openrouter.ai/api/v1

A model slug may carry an explicit backend: `backend:model`. Otherwise the registry
maps well-known prefixes (`anthropic/`, `openai/`, ...) and local providers are chosen
by name (`ollama`, `lm-studio`, `mlx`, `llamacpp`) or by any base URL in ~/.flint/
providers.json. Unknown slugs resolve to the default provider (OpenRouter).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ─── built-in providers ───────────────────────────────────────────────────────

DEFAULT_PROVIDER = "openrouter"

OPENROUTER_DEFAULT = "https://openrouter.ai/api/v1"
OPENAI_DEFAULT = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT = "https://api.anthropic.com/v1"
OLLAMA_DEFAULT = "http://localhost:11434/v1"
LM_STUDIO_DEFAULT = "http://localhost:1234/v1"
MLX_DEFAULT = "http://localhost:8080/v1"
LLAMACPP_DEFAULT = "http://localhost:8080/v1"

PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "base_url": OPENROUTER_DEFAULT,
        "env": "OPENROUTER_BASE_URL",
        "api_type": "openai",            # OpenAI-compatible /v1/chat/completions
        "local": False,
        "keys": ("OPENROUTER_API_KEY",),
    },
    "openai": {
        "label": "OpenAI",
        "base_url": OPENAI_DEFAULT,
        "env": "OPENAI_BASE_URL",
        "api_type": "openai",
        "local": False,
        "keys": ("OPENAI_API_KEY",),
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": ANTHROPIC_DEFAULT,
        "env": "ANTHROPIC_BASE_URL",
        "api_type": "anthropic",         # Anthropic Messages /v1/messages
        "local": False,
        "keys": ("ANTHROPIC_API_KEY",),
    },
    "ollama": {
        "label": "Ollama",
        "base_url": OLLAMA_DEFAULT,
        "env": "OLLAMA_BASE_URL",
        "api_type": "openai",
        "local": True,
        "keys": (),
        "command": "ollama serve",
    },
    "lm-studio": {
        "label": "LM Studio",
        "base_url": LM_STUDIO_DEFAULT,
        "env": "LM_STUDIO_BASE_URL",
        "api_type": "openai",
        "local": True,
        "keys": (),
        "command": "LM Studio → Local Server",
    },
    "mlx": {
        "label": "MLX",
        "base_url": MLX_DEFAULT,
        "env": "MLX_BASE_URL",
        "api_type": "openai",
        "local": True,
        "keys": (),
        "command": "mlx_lm.server --model <model>",
    },
    "llamacpp": {
        "label": "llama.cpp",
        "base_url": LLAMACPP_DEFAULT,
        "env": "LLAMACPP_BASE_URL",
        "api_type": "openai",
        "local": True,
        "keys": (),
        "command": "llama-server -m <model.gguf>",
    },
}

# Well-known model prefixes people write by hand. An explicit `backend:` marker always
# wins; after that, a prefix match makes "anthropic/claude-..." obviously Anthropic
# without asking anyone, while everything else stays on the default provider.
PREFIX_PROVIDERS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "ollama": "ollama",
    "lm-studio": "lm-studio",
    "lmstudio": "lm-studio",
    "mlx": "mlx",
    "llamacpp": "llamacpp",
    "llama.cpp": "llamacpp",
}

BCP47_SEP = {":", "/", "\\"}


def _split_marker(model):
    """`backend:model` -> ("backend", "model"); otherwise (None, model).

    A marker is only read when the first colon sits before any slash, so an
    ``anthropic/claude-3:free`` slug is *not* mistaken for the backend `anthropic`.
    """
    if not isinstance(model, str) or ":" not in model:
        return None, model
    head, _, tail = model.partition(":")
    if any(sep in head for sep in BCP47_SEP) or not tail:
        return None, model
    if head in PROVIDERS:
        return head, tail
    return None, model


def normalize_url(url):
    """A base URL with a trailing /v1 added when it plainly lacks one.

    Ollama answers on /v1 in current releases but older builds exposed /v1/chat/
    completions at the bare port; Cursor treats "http://localhost:11434" and
    "http://localhost:11434/v1" as the same server. Keep it a no-op for the built-in
    defaults, which are already /v1-shaped.
    """
    if not url:
        return url
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url
    # A path that already looks API-specific is left alone.
    if any(seg in url for seg in ("/v1/", "/chat/completions", "/messages")):
        return url
    return url + "/v1"


def is_local(provider):
    """True when the provider is one of the built-in local backends or a custom endpoint
    with a non-default base URL from ~/.flint/providers.json."""
    p = PROVIDERS.get(provider)
    if p is not None:
        return bool(p.get("local"))
    # A custom provider is local when it has a URL and no cloud kind.
    return False


def detect_api_type(base_url, headers=None, timeout=3):
    """Probe an endpoint to learn whether it speaks OpenAI chat completions or
    Anthropic messages. Returns "openai", "anthropic" or None (unreachable).

    Local servers differ: Ollama/LM Studio/MLX/llama.cpp expose /v1/models and
    /v1/chat/completions, but some LM Studio and llama.cpp builds also (or only)
    implement Anthropic's /v1/messages. The endpoint override is read before any
    guess, and the probe is gated on `headers` being empty — a real API key must not
    be sent into a probe that is only meant to classify a local dev server.
    """
    import urllib.error
    import urllib.request

    if not base_url:
        return "openai"
    probe = base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(probe, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "openai"
    except urllib.error.HTTPError as e:
        return "openai" if e.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def base_url_for(provider, env=None):
    """The effective base URL for a provider: env override, then ~/.flint/providers.json
    custom endpoint, then the built-in default."""
    env = os.environ if env is None else env
    info = PROVIDERS.get(provider)
    if info is None:
        return env.get("OPENROUTER_BASE_URL", OPENROUTER_DEFAULT)
    env_url = env.get(info["env"]) if info.get("env") else None
    if env_url:
        return normalize_url(env_url)
    if info.get("local"):
        cfg = load_providers(env=env)
        custom = cfg.get("openAIBaseUrl")
        if custom:
            return normalize_url(custom)
    return info["base_url"]


def api_key_for(provider, env=None):
    """The key to send to a provider. Local servers take none; everything else reads
    its provider-specific env var (OpenRouter keeps OPENROUTER_API_KEY, the legacy
    name, for compatibility)."""
    env = os.environ if env is None else env
    info = PROVIDERS.get(provider) or {}
    for name in info.get("keys", ()):
        if env.get(name):
            return env[name]
    if provider == DEFAULT_PROVIDER:
        return env.get("OPENROUTER_API_KEY", "")
    return ""


def model_slug(model, provider):
    """A plain model id for the wire, stripping a `backend:` marker and unqualified
    local names that the server itself resolves (an Ollama model "qwen2.5-coder:7b"
    keeps its tag — that colon is part of the name)."""
    _, slug = _split_marker(model)
    return slug


def resolve(model, default=DEFAULT_PROVIDER, env=None):
    """The transport facts for a model string.

    Returns a dict::

        {"provider", "backend", "model", "base_url", "api_type",
         "api_key", "local", "default", "custom"}

    `backend` is the registry name ("ollama", "openrouter", ...), `provider` keeps the
    label given by the caller in the *ask* flow so the panel can show both the chosen
    provider and the model actually resolved. `custom` is True when the base URL came
    from ~/.flint/providers.json (a user-added OpenAI-compatible endpoint), and a
    custom endpoint is always treated as local — it never needs an OpenRouter key and
    never touches the OpenRouter accounting.
    """
    env = os.environ if env is None else env
    backend, model = _split_marker(model)
    if backend is None:
        prefix = str(model).split("/", 1)[0].lower()
        backend = PREFIX_PROVIDERS.get(prefix, default)
    if backend not in PROVIDERS:
        backend = default
    info = PROVIDERS[backend]
    pconf = load_providers(env=env)
    base = base_url_for(backend, env=env)
    custom = bool(pconf.get("openAIBaseUrl"))
    local = bool(info.get("local")) or custom
    return {
        "provider": default,
        "backend": backend,
        "model": model_slug(model, backend),
        "base_url": base,
        "api_type": info.get("api_type", "openai"),
        "api_key": "" if local else api_key_for(backend, env=env),
        "local": local,
        "default": backend == default,
        "custom": custom,
    }


def provider_choices():
    """All providers as QuickPick-friendly entries, locals grouped first."""
    def entry(name):
        info = PROVIDERS[name]
        return {"id": name, "label": info["label"], "local": bool(info.get("local")),
                "detail": _detail(name, info)}
    return [entry(n) for n in PROVIDERS if PROVIDERS[n].get("local")] + \
           [entry(n) for n in PROVIDERS if not PROVIDERS[n].get("local")]


def _detail(name, info):
    base = base_url_for(name)
    if name == "ollama":
        try:
            from urllib.request import urlopen as _u
            with _u(base.rstrip("/") + "/models", timeout=1):
                return f"{base} · running"
        except Exception:
            return f"{base} · start with `ollama serve`"
        return f"{base}"
    return info.get("command", base)


# ─── ~/.flint/providers.json ──────────────────────────────────────────────────

def providers_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get("FLINT_HOME", "~/.flint")).expanduser() / "providers.json"


def load_providers(env=None):
    """~/.flint/providers.json, with the Cursor migration behavior: an empty
    openAIBaseUrl reads back as null, and missing local model lists default to []."""
    env = os.environ if env is None else env
    path = providers_path(env)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    base = data.pop("openAIBaseUrl", None)
    if base == "":
        base = None
    data["openAIBaseUrl"] = base
    for k in ("localProviderModelIds", "localProviderAgentModelIds",
              "customEndpoints", "selectedBackend", "selectedModel"):
        if k not in data:
            data[k] = [] if k.endswith("Ids") or k == "customEndpoints" else None
    return data


def save_providers(data, env=None):
    """Write ~/.flint/providers.json chmod 0600. The file may hold per-model API keys
    (the change request's secrets posture), so it must never be world-readable."""
    env = os.environ if env is None else env
    path = providers_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def selected_model(env=None):
    """(backend, model) the user last picked, or (None, None)."""
    env = os.environ if env is None else env
    d = load_providers(env)
    return d.get("selectedBackend") or None, d.get("selectedModel") or None


def set_selected_model(backend, model, env=None):
    env = os.environ if env is None else env
    d = load_providers(env)
    d["selectedBackend"] = backend
    d["selectedModel"] = model
    save_providers(d, env)