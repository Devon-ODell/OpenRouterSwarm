"""SQLite persistence kept deliberately boring and portable."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS influencers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL UNIQUE,
  niche TEXT NOT NULL, bio TEXT NOT NULL, voice TEXT NOT NULL,
  appearance TEXT NOT NULL, values_json TEXT NOT NULL,
  boundaries TEXT NOT NULL, disclosure TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, influencer_id TEXT NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
  concept TEXT NOT NULL, caption TEXT NOT NULL, image_prompt TEXT NOT NULL,
  image_url TEXT NOT NULL, model TEXT NOT NULL, cost REAL,
  ai_generated INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, influencer_id TEXT NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
  fan_id TEXT NOT NULL, fan_name TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread
  ON messages(influencer_id, fan_id, created_at);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _id(prefix):
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _influencer(row):
        if row is None:
            return None
        item = dict(row)
        item["values"] = json.loads(item.pop("values_json"))
        return item

    def list_influencers(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM influencers ORDER BY created_at DESC").fetchall()
        return [self._influencer(r) for r in rows]

    def get_influencer(self, influencer_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM influencers WHERE id = ?", (influencer_id,)).fetchone()
        return self._influencer(row)

    def create_influencer(self, item):
        item = dict(item, id=self._id("inf"), created_at=int(time.time()))
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO influencers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item["name"], item["handle"], item["niche"], item["bio"],
                 item["voice"], item["appearance"], json.dumps(item["values"]),
                 item["boundaries"], item["disclosure"], item["created_at"]),
            )
        return item

    def list_posts(self, influencer_id=None):
        sql = "SELECT * FROM posts"
        args = ()
        if influencer_id:
            sql += " WHERE influencer_id = ?"
            args = (influencer_id,)
        sql += " ORDER BY created_at DESC"
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def create_post(self, item):
        item = dict(item, id=self._id("post"), ai_generated=1, created_at=int(time.time()))
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item["influencer_id"], item["concept"], item["caption"],
                 item["image_prompt"], item["image_url"], item["model"], item.get("cost"),
                 1, item["created_at"]),
            )
        return item

    def add_message(self, influencer_id, fan_id, fan_name, role, content):
        item = {"id": self._id("msg"), "influencer_id": influencer_id, "fan_id": fan_id,
                "fan_name": fan_name, "role": role, "content": content, "created_at": time.time_ns()}
        with self._lock, self.connect() as db:
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
        return item

    def thread(self, influencer_id, fan_id, limit=24):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE influencer_id = ? AND fan_id = ? "
                "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC",
                (influencer_id, fan_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def conversations(self, influencer_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT m.* FROM messages m JOIN (SELECT fan_id, MAX(rowid) rid FROM messages "
                "WHERE influencer_id = ? GROUP BY fan_id) x ON m.rowid = x.rid ORDER BY m.created_at DESC",
                (influencer_id,),
            ).fetchall()
        return [dict(r) for r in rows]
