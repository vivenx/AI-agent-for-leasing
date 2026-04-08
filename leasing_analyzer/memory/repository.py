from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


class MemoryRepository:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    user_input TEXT NOT NULL,
                    response_summary TEXT NOT NULL,
                    item_name TEXT,
                    price INTEGER,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)

    def ensure_session(self, session_id: str, user_id: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO sessions(session_id, user_id)
                VALUES(?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = COALESCE(excluded.user_id, sessions.user_id),
                    updated_at = CURRENT_TIMESTAMP
            """, (session_id, user_id))

    def add_interaction(
        self,
        session_id: str,
        kind: str,
        user_input: str,
        response_summary: str,
        item_name: Optional[str] = None,
        price: Optional[int] = None,
        metadata_json: str = "{}",
    ) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO interactions(
                    session_id, kind, user_input, response_summary, item_name, price, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, kind, user_input, response_summary, item_name, price, metadata_json))
            conn.execute("""
                UPDATE sessions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
            """, (session_id,))

    def get_recent_interactions(self, session_id: str, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT kind, user_input, response_summary, item_name, price, created_at
                FROM interactions
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            """, (session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def find_related_by_item(self, session_id: str, item_name: str, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT kind, user_input, response_summary, item_name, price, created_at
                FROM interactions
                WHERE session_id = ?
                  AND item_name IS NOT NULL
                  AND LOWER(item_name) LIKE LOWER(?)
                ORDER BY id DESC
                LIMIT ?
            """, (session_id, f"%{item_name}%", limit)).fetchall()
        return [dict(row) for row in rows]

    def upsert_summary(self, session_id: str, summary: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO session_summaries(session_id, summary)
                VALUES(?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = CURRENT_TIMESTAMP
            """, (session_id, summary))

    def get_summary(self, session_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT summary
                FROM session_summaries
                WHERE session_id = ?
            """, (session_id,)).fetchone()
        return row["summary"] if row else None
