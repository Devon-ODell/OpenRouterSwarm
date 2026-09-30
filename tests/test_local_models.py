"""Local model discovery: `local-models` against a fake /v1/models server.

The editor's provider dropdown depends on the bridge listing what a local server has pulled.
These tests stand up a real http.server on a free port, point a provider's base URL at it,
and check the bridge returns a provider with the right running flag and model list — without
Ollama or any real local model being installed.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'swarm'))
import providers


def _venv_python():
    """The repo's interpreter when it exists; the bridge needs dotenv/openai."""
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


class FakeLocalServer:
    """Serves GET /v1/models with a small fixed model list."""

    def __init__(self, models):
        self.models = models
        handler = self._handler_cls()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler_cls(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    body = json.dumps({"object": "list", "data": outer.models}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        return H

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


class LocalModelsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ)
        self.env["FLINT_HOME"] = self.tmp.name

    def run_bridge(self, *args):
        env = dict(self.env)
        # The bridge child must see the sandbox off so it can spawn flint if asked.
        env.pop("FLINT_SWARM_BUDGET", None)
        p = subprocess.run([self.env.get("FLINT_PYTHON", _venv_python()),
                            str(ROOT / "swarm" / "bridge.py"), *args],
                           capture_output=True, text=True, env=env, timeout=30)
        out = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        return out, p

    def test_local_models_lists_running_provider(self):
        with FakeLocalServer([{"id": "qwen2.5-coder:7b", "name": "Qwen 2.5 Coder 7B", "context_length": 32768},
                              {"id": "llama3.1:8b", "name": "Llama 3.1 8B"}]) as srv:
            self.env["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
            out, p = self.run_bridge("local-models")
        self.assertEqual(p.returncode, 0)
        ollama = next((pr for pr in out[-1]["providers"] if pr["id"] == "ollama"), None)
        self.assertIsNotNone(ollama)
        self.assertTrue(ollama["running"])
        ids = [m["id"] for m in ollama["models"]]
        self.assertIn("qwen2.5-coder:7b", ids)
        self.assertIn("llama3.1:8b", ids)

    def test_local_models_unreachable_is_not_running(self):
        out, p = self.run_bridge("local-models")
        self.assertEqual(p.returncode, 0)
        ollama = next((pr for pr in out[-1]["providers"] if pr["id"] == "ollama"), None)
        self.assertIsNotNone(ollama)
        self.assertFalse(ollama["running"])
        self.assertEqual(ollama["models"], [])

    def test_custom_endpoint_appears_after_persistence(self):
        with FakeLocalServer([{"id": "remote-model", "name": "Remote Model"}]) as srv:
            providers.save_providers({
                "customEndpoints": [{"label": "my box", "baseUrl": f"http://127.0.0.1:{srv.port}/v1"}],
            }, self.env)
            out, p = self.run_bridge("local-models")
        custom = [pr for pr in out[-1]["providers"] if pr["id"].startswith("custom:")]
        self.assertEqual(len(custom), 1)
        self.assertTrue(custom[0]["running"])
        self.assertEqual(custom[0]["models"][0]["id"], "remote-model")


class NoOpenRouterOnLocalAskTests(unittest.TestCase):
    """A local ask must not need an OpenRouter key and must not fall back to the wallet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ)
        self.env["FLINT_HOME"] = self.tmp.name
        self.env.pop("OPENROUTER_API_KEY", None)

    def test_resolve_local_without_key(self):
        r = providers.resolve("ollama:qwen2.5-coder:7b", env=self.env)
        self.assertTrue(r["local"])
        self.assertEqual(r["api_key"], "")

    def test_flint_agent_local_needs_no_key(self):
        env = dict(self.env)
        # Point Ollama somewhere that is down; the agent must still construct (no key error).
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:9/v1"
        code = subprocess.run(
            [_venv_python(), "-c",
             "import sys; sys.path.insert(0, '.'); import flint; "
             "a = flint.Agent('ollama:qwen2.5-coder:7b', headless=True, read_only=True); "
             "assert a.local and a.api_key == '', (a.local, a.api_key)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30).returncode
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()