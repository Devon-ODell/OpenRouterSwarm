"""Provider registry: resolution, env overrides, persistence, and local detection.

The headline Phase 1 feature is a model string routing to a local or cloud backend. These
tests pin the resolver's contract so the editor's picker and flint's Agent agree about what
a `--provider ollama --model qwen2.5-coder:7b` ask means before any socket is opened.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm'))
import providers


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, FLINT_HOME=self.tmp.name)
        # Local providers read a keyless users.json that must not leak from the real home dir.
        self.env.pop("OPENROUTER_API_KEY", None)

    def resolve(self, model, default="openrouter", **kw):
        return providers.resolve(model, default=default, env=self.env)

    def test_openrouter_slug_stays_on_openrouter(self):
        r = self.resolve("inclusionai/ling-3.0-flash-fin:free")
        self.assertEqual(r["backend"], "openrouter")
        self.assertEqual(r["base_url"], "https://openrouter.ai/api/v1")
        self.assertFalse(r["local"])
        self.assertTrue(r["default"])

    def test_explicit_backend_marker_wins(self):
        r = self.resolve("ollama:qwen2.5-coder:7b")
        self.assertEqual(r["backend"], "ollama")
        self.assertEqual(r["model"], "qwen2.5-coder:7b")   # the :7b tag is the model name, not a marker
        self.assertEqual(r["base_url"], "http://localhost:11434/v1")
        self.assertTrue(r["local"])

    def test_vendor_prefix_selects_anthropic(self):
        r = self.resolve("anthropic/claude-sonnet-4.5")
        self.assertEqual(r["backend"], "anthropic")
        self.assertEqual(r["api_type"], "anthropic")
        self.assertFalse(r["local"])

    def test_free_slug_colon_is_not_a_backend_marker(self):
        # `inclusionai/ling-3.0-flash-fin:free` must not parse as backend `inclusionai`.
        r = self.resolve("inclusionai/ling-3.0-flash-fin:free")
        self.assertEqual(r["backend"], "openrouter")
        self.assertEqual(r["model"], "inclusionai/ling-3.0-flash-fin:free")

    def test_env_override_beats_default(self):
        self.env["OLLAMA_BASE_URL"] = "http://192.168.1.20:11434"
        r = self.resolve("ollama:qwen2.5-coder:7b")
        self.assertEqual(r["base_url"], "http://192.168.1.20:11434/v1")

    def test_custom_base_url_is_treated_as_local(self):
        d = {"openAIBaseUrl": "http://localhost:5678/v1"}
        with open(Path(self.tmp.name) / "providers.json", "w") as f:
            json.dump(d, f)
        r = self.resolve("some/local-model")
        self.assertTrue(r["local"])

    def test_provider_not_in_registry_falls_back_to_default(self):
        r = self.resolve("garbage/model", default="ollama")
        self.assertEqual(r["backend"], "ollama")

    def test_unknown_provider_names_fall_back_to_openrouter(self):
        r = self.resolve("foo:bar", default="openrouter")
        self.assertEqual(r["backend"], "openrouter")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, FLINT_HOME=self.tmp.name)

    def test_empty_providers_json_reads_with_cursor_keys(self):
        d = providers.load_providers(self.env)
        self.assertIsNone(d["openAIBaseUrl"])
        self.assertEqual(d["localProviderModelIds"], [])
        self.assertEqual(d["localProviderAgentModelIds"], [])
        self.assertIsNone(d["selectedModel"])

    def test_empty_open_ai_base_url_reads_as_null(self):
        path = Path(self.tmp.name) / "providers.json"
        path.write_text(json.dumps({"openAIBaseUrl": ""}))
        self.assertIsNone(providers.load_providers(self.env)["openAIBaseUrl"])

    def test_save_writes_0600(self):
        path = providers.save_providers({"openAIBaseUrl": "http://localhost:9999/v1"}, self.env)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode & 0o077, 0)      # no group/other bits

    def test_selected_model_round_trips(self):
        providers.set_selected_model("ollama", "qwen2.5-coder:7b", self.env)
        self.assertEqual(providers.selected_model(self.env), ("ollama", "qwen2.5-coder:7b"))


class LocalModelListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, OLLAMA_BASE_URL="", LM_STUDIO_BASE_URL="")

    def test_detect_api_type_openai(self):
        # A server answering /v1/models is OpenAI-compatible; flint probes this before deciding.
        self.assertEqual(providers.detect_api_type("http://127.0.0.1:1"), None)  # unreachable -> None
        self.assertEqual(providers.detect_api_type(""), "openai")               # no URL -> assume openai


if __name__ == "__main__":
    unittest.main()