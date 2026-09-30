import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ["INFLUENCER_MOCK"] = "1"

from influencer_studio.app import App, DISCLOSURE  # noqa: E402
from influencer_studio.openrouter import OpenRouter  # noqa: E402
from influencer_studio.safety import SafetyError  # noqa: E402
from influencer_studio.store import Store  # noqa: E402
from server import Handler, make_server  # noqa: E402


PERSONA = {
    "name": "Mira Vale",
    "handle": "miravale.ai",
    "niche": "slow travel and architecture",
    "bio": "Quiet places and considered design for curious travelers.",
    "voice": "Warm, observant, concise, and lightly witty.",
    "appearance": "An original fictional 28-year-old adult with a dark curly bob and copper glasses",
    "values": ["curiosity", "transparency", "sustainability"],
    "boundaries": "No romance, private meetups, professional advice, or requests for money.",
}


class StudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "studio.db")
        self.models = OpenRouter(root / "media")
        self.app = App(self.store, self.models)

    def tearDown(self):
        self.temp.cleanup()

    def test_persona_post_and_chat_round_trip(self):
        persona = self.app.create_influencer(PERSONA)
        self.assertEqual(persona["disclosure"], DISCLOSURE)
        self.assertEqual(self.store.get_influencer(persona["id"])["values"], PERSONA["values"])

        post = self.app.generate_post(persona["id"], {"concept": "A rainy morning café guide", "aspect_ratio": "4:5"})
        self.assertTrue(post["ai_generated"])
        self.assertEqual(post["model"], "openai/gpt-image-1-mini")
        self.assertTrue((Path(self.temp.name) / post["image_url"].lstrip("/")).is_file())

        first = self.app.chat(persona["id"], {"fan_id": "fan-1", "fan_name": "Maya", "message": "I love brutalist cafés"})
        self.assertEqual(first["reply"]["role"], "influencer")
        second = self.app.chat(persona["id"], {"fan_id": "fan-1", "fan_name": "Maya", "message": "Any favorites?"})
        self.assertEqual(len(self.store.thread(persona["id"], "fan-1")), 4)
        self.assertIn("Any favorites?", second["reply"]["content"])

    def test_rejects_unsafe_or_impersonating_personas(self):
        for appearance in ("a schoolgirl fashion creator", "looks exactly like a famous actor"):
            with self.subTest(appearance=appearance), self.assertRaises(SafetyError):
                self.app.create_influencer(dict(PERSONA, handle=f"blocked{len(appearance)}", appearance=appearance))

    def test_handle_is_normalized_and_unique(self):
        first = self.app.create_influencer(dict(PERSONA, handle="@Mira.Vale"))
        self.assertEqual(first["handle"], "mira.vale")
        with self.assertRaises(Exception):
            self.app.create_influencer(dict(PERSONA, handle="mira.vale"))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_and_workflow_routes(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual((status, health["mode"]), (200, "demo"))
        status, created = self.request("POST", "/api/influencers", PERSONA)
        self.assertEqual(status, 201)
        influencer_id = created["influencer"]["id"]
        status, post = self.request("POST", f"/api/influencers/{influencer_id}/posts/generate",
                                    {"concept": "A library built into a hillside", "aspect_ratio": "1:1"})
        self.assertEqual(status, 201)
        self.assertTrue(post["post"]["image_url"].startswith("/media/"))
        status, chat = self.request("POST", f"/api/influencers/{influencer_id}/chat",
                                    {"fan_id": "f1", "fan_name": "Sam", "message": "This is beautiful"})
        self.assertEqual(status, 201)
        self.assertEqual(chat["reply"]["role"], "influencer")

    def test_bad_json_is_a_client_error(self):
        req = urllib.request.Request(self.base + "/api/influencers", data=b"nope", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
