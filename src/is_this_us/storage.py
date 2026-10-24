"""SQLite storage: pairing tokens, visitor sessions, interactions, saved memories.

Privacy by design:
* No images, no landmarks, no names. Interactions keep only coarse labels
  (posture, artwork id, robot behavior) and expire after SESSION_TTL.
* A visitor is identified only by their Telegram chat id, which is needed to reply.
* `forget(chat_id)` removes every row linked to that chat.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairing_tokens (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visitors (
    chat_id INTEGER PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'en',
    paired_until TEXT,
    created_at TEXT NOT NULL,
    archive TEXT
);
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    origin TEXT NOT NULL,              -- 'installation' | 'photo'
    posture TEXT NOT NULL,
    posture_confidence REAL NOT NULL,
    artwork_id TEXT,
    shown_ids TEXT NOT NULL DEFAULT '[]',
    behavior TEXT,
    status TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    archive TEXT NOT NULL DEFAULT 'art'
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    artwork_id TEXT NOT NULL,
    interaction_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, artwork_id)
);
CREATE INDEX IF NOT EXISTS ix_inter_chat ON interactions(chat_id);
CREATE INDEX IF NOT EXISTS ix_mem_chat ON memories(chat_id);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass
class InteractionRow:
    id: str
    chat_id: int
    origin: str
    posture: str
    posture_confidence: float
    artwork_id: str | None
    shown_ids: list[str]
    behavior: str | None
    status: str | None
    created_at: str
    expires_at: str
    observation: dict
    archive: str = "art"

    @property
    def expired(self) -> bool:
        return datetime.fromisoformat(self.expires_at) < _now()


class Storage:
    def __init__(self, path: Path | str, *, pairing_ttl_min: int = 10, session_ttl_min: int = 30):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()
        self.pairing_ttl = timedelta(minutes=pairing_ttl_min)
        self.session_ttl = timedelta(minutes=session_ttl_min)

    def _migrate(self) -> None:
        """Add columns introduced after v0.2 to existing databases."""
        for table, col, decl in (("visitors", "archive", "TEXT"),
                                 ("interactions", "archive", "TEXT NOT NULL DEFAULT 'art'")):
            cols = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    # ------------------------------------------------------------------ pairing
    def create_pairing_token(self) -> str:
        token = secrets.token_urlsafe(9)  # 12 url-safe chars, fits Telegram /start payload (<=64)
        now = _now()
        self._exec("INSERT INTO pairing_tokens VALUES (?,?,?)", (token, _iso(now), _iso(now + self.pairing_ttl)))
        return token

    def pair(self, chat_id: int, token: str, lang: str = "en") -> bool:
        row = self._exec("SELECT expires_at FROM pairing_tokens WHERE token=?", (token,)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) < _now():
            return False
        self.touch_visitor(chat_id, lang)
        self._exec("UPDATE visitors SET paired_until=? WHERE chat_id=?", (_iso(_now() + self.session_ttl), chat_id))
        return True

    def is_paired(self, chat_id: int) -> bool:
        row = self._exec("SELECT paired_until FROM visitors WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["paired_until"] and datetime.fromisoformat(row["paired_until"]) > _now())

    def unpair(self, chat_id: int) -> None:
        self._exec("UPDATE visitors SET paired_until=NULL WHERE chat_id=?", (chat_id,))

    # ------------------------------------------------------------------ visitors
    def touch_visitor(self, chat_id: int, lang: str = "en") -> None:
        self._exec(
            "INSERT INTO visitors(chat_id, lang, created_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang",
            (chat_id, lang, _iso(_now())),
        )

    def visitor_lang(self, chat_id: int, default: str = "en") -> str:
        row = self._exec("SELECT lang FROM visitors WHERE chat_id=?", (chat_id,)).fetchone()
        return row["lang"] if row else default

    def set_visitor_archive(self, chat_id: int, archive: str) -> None:
        self.touch_visitor(chat_id, self.visitor_lang(chat_id))
        self._exec("UPDATE visitors SET archive=? WHERE chat_id=?", (archive, chat_id))

    def visitor_archive(self, chat_id: int, default: str = "both") -> str:
        row = self._exec("SELECT archive FROM visitors WHERE chat_id=?", (chat_id,)).fetchone()
        return (row["archive"] if row and row["archive"] else default)

    # ------------------------------------------------------------------ interactions
    def new_interaction_id(self) -> str:
        return secrets.token_hex(4)

    def save_interaction(self, *, interaction_id: str, chat_id: int, origin: str, posture: str,
                         posture_confidence: float, artwork_id: str | None, shown_ids: list[str],
                         behavior: str | None, status: str | None, observation: dict,
                         archive: str = "art") -> None:
        now = _now()
        self._exec(
            "INSERT OR REPLACE INTO interactions (id, chat_id, origin, posture, posture_confidence, artwork_id, "
            "shown_ids, behavior, status, created_at, expires_at, observation_json, archive) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (interaction_id, chat_id, origin, posture, posture_confidence, artwork_id, json.dumps(shown_ids),
             behavior, status, _iso(now), _iso(now + self.session_ttl), json.dumps(observation), archive),
        )

    def get_interaction(self, interaction_id: str, chat_id: int) -> InteractionRow | None:
        """Fetch only if it belongs to this chat — never leak another visitor's moment."""
        row = self._exec("SELECT * FROM interactions WHERE id=? AND chat_id=?", (interaction_id, chat_id)).fetchone()
        if not row:
            return None
        return InteractionRow(
            id=row["id"], chat_id=row["chat_id"], origin=row["origin"], posture=row["posture"],
            posture_confidence=row["posture_confidence"], artwork_id=row["artwork_id"],
            shown_ids=json.loads(row["shown_ids"]), behavior=row["behavior"], status=row["status"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            observation=json.loads(row["observation_json"]), archive=row["archive"] or "art",
        )

    def has_seen_meme(self, chat_id: int) -> bool:
        return self._exec("SELECT 1 FROM interactions WHERE chat_id=? AND artwork_id LIKE 'meme:%' LIMIT 1",
                          (chat_id,)).fetchone() is not None

    def count_interactions(self, chat_id: int) -> int:
        return self._exec("SELECT COUNT(*) FROM interactions WHERE chat_id=?", (chat_id,)).fetchone()[0]

    def recent_artworks(self, chat_id: int, limit: int = 10) -> list[str]:
        rows = self._exec("SELECT shown_ids FROM interactions WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                          (chat_id, limit)).fetchall()
        seen: list[str] = []
        for r in rows:
            seen.extend(json.loads(r["shown_ids"]))
        return seen

    # ------------------------------------------------------------------ memories
    def keep_memory(self, chat_id: int, artwork_id: str, interaction_id: str | None) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO memories(chat_id, artwork_id, interaction_id, created_at) "
                         "VALUES (?,?,?,?)", (chat_id, artwork_id, interaction_id, _iso(_now())))
        return cur.rowcount > 0

    def memories(self, chat_id: int) -> list[tuple[str, str]]:
        rows = self._exec("SELECT artwork_id, created_at FROM memories WHERE chat_id=? ORDER BY created_at",
                          (chat_id,)).fetchall()
        return [(r["artwork_id"], r["created_at"]) for r in rows]

    # ------------------------------------------------------------------ privacy
    def forget(self, chat_id: int) -> None:
        for table in ("interactions", "memories", "visitors"):
            self._exec(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))

    def cleanup(self) -> int:
        """Delete expired interactions and pairing tokens. Saved memories are kept until /forget."""
        now = _iso(_now())
        n = self._exec("DELETE FROM interactions WHERE expires_at < ?", (now,)).rowcount
        self._exec("DELETE FROM pairing_tokens WHERE expires_at < ?", (now,))
        self._exec("UPDATE visitors SET paired_until=NULL WHERE paired_until < ?", (now,))
        return n

    def close(self) -> None:
        self._db.close()
