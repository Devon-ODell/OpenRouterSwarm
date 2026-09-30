#!/usr/bin/env python3
"""Run Influencer Studio with only the Python standard library."""

import argparse
import json
import mimetypes
import os
import sqlite3
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from influencer_studio.app import App
from influencer_studio.openrouter import ModelError, OpenRouter
from influencer_studio.safety import SafetyError
from influencer_studio.store import Store


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("INFLUENCER_DATA_DIR", ROOT / "data")).expanduser().resolve()


class Handler(SimpleHTTPRequestHandler):
    app = None
    models = None

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SafetyError("Invalid Content-Length") from exc
        if size <= 0 or size > 100_000:
            raise SafetyError("Request body must be between 1 byte and 100 KB")
        try:
            body = json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SafetyError("Request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise SafetyError("Request body must be a JSON object")
        return body

    def _parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def do_GET(self):
        parts = self._parts()
        try:
            if parts == ["api", "health"]:
                return self._json(200, {"ok": True, "mode": "demo" if self.models.mock else "live",
                                        "chat_model": self.models.chat_model,
                                        "image_model": self.models.image_model})
            if parts == ["api", "influencers"]:
                return self._json(200, {"influencers": self.app.store.list_influencers()})
            if parts == ["api", "posts"]:
                return self._json(200, {"posts": self.app.store.list_posts()})
            if len(parts) == 4 and parts[:2] == ["api", "influencers"] and parts[3] == "posts":
                return self._json(200, {"posts": self.app.store.list_posts(parts[2])})
            if len(parts) == 4 and parts[:2] == ["api", "influencers"] and parts[3] == "conversations":
                return self._json(200, {"conversations": self.app.store.conversations(parts[2])})
            if len(parts) == 5 and parts[:2] == ["api", "influencers"] and parts[3] == "chats":
                return self._json(200, {"messages": self.app.store.thread(parts[2], parts[4], 100)})
            if parts and parts[0] == "media":
                return self._serve_file(DATA / "media", parts[1:])
            if not parts:
                return self._serve_file(STATIC, ["index.html"])
            return self._serve_file(STATIC, parts)
        except (OSError, KeyError) as exc:
            self._error(exc)

    def do_POST(self):
        parts = self._parts()
        try:
            body = self._body()
            if parts == ["api", "influencers"]:
                return self._json(HTTPStatus.CREATED, {"influencer": self.app.create_influencer(body)})
            if len(parts) == 5 and parts[:2] == ["api", "influencers"] and parts[3:] == ["posts", "generate"]:
                return self._json(HTTPStatus.CREATED, {"post": self.app.generate_post(parts[2], body)})
            if len(parts) == 4 and parts[:2] == ["api", "influencers"] and parts[3] == "chat":
                return self._json(HTTPStatus.CREATED, self.app.chat(parts[2], body))
            return self._json(404, {"error": "Route not found"})
        except (SafetyError, ValueError, sqlite3.IntegrityError) as exc:
            message = "That handle is already in use" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
            self._json(400, {"error": message})
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except ModelError as exc:
            self._json(502, {"error": str(exc)})
        except Exception as exc:  # keep raw internals out of HTTP responses
            print(f"Unhandled request error: {type(exc).__name__}: {exc}")
            self._json(500, {"error": "Unexpected server error"})

    def _serve_file(self, root, parts):
        root = root.resolve()
        target = root.joinpath(*parts).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._json(403, {"error": "Forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "Not found"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc):
        self._json(404 if isinstance(exc, KeyError) else 500, {"error": str(exc).strip("'")})


def make_server(host="127.0.0.1", port=8787, data_dir=DATA):
    store = Store(Path(data_dir) / "studio.db")
    models = OpenRouter(Path(data_dir) / "media")
    Handler.app, Handler.models = App(store, models), models
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Synthetic influencer studio and fan inbox")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"Influencer Studio: http://{args.host}:{server.server_port}")
    print(f"Mode: {'offline demo' if Handler.models.mock else 'live OpenRouter'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
