from __future__ import annotations

import sqlite3
from hashlib import sha256
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dataset_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    user_id TEXT,
                    dataset_name TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    item_name TEXT,
                    price INTEGER,
                    source_kind TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
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

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT session_id, user_id, created_at, updated_at
                FROM sessions
                WHERE session_id = ?
            """, (session_id,)).fetchone()
        return dict(row) if row else None

    def get_all_interactions(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT kind, user_input, response_summary, item_name, price, metadata_json, created_at
                FROM interactions
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            """, (session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def delete_session_memory(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM interactions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM dataset_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def add_dataset_entry(
        self,
        dataset_name: str,
        entry_type: str,
        title: str,
        content: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        item_name: Optional[str] = None,
        price: Optional[int] = None,
        source_kind: Optional[str] = None,
        metadata_json: str = "{}",
    ) -> None:
        fingerprint = sha256(
            "|".join(
                [
                    dataset_name.strip().lower(),
                    entry_type.strip().lower(),
                    title.strip().lower(),
                    content.strip().lower(),
                    (session_id or "").strip().lower(),
                    (user_id or "").strip().lower(),
                    (item_name or "").strip().lower(),
                    str(price or ""),
                    (source_kind or "").strip().lower(),
                ]
            ).encode("utf-8")
        ).hexdigest()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dataset_entries(
                    session_id, user_id, dataset_name, entry_type, title, content,
                    item_name, price, source_kind, fingerprint, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    content = excluded.content,
                    price = excluded.price,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    user_id,
                    dataset_name,
                    entry_type,
                    title,
                    content,
                    item_name,
                    price,
                    source_kind,
                    fingerprint,
                    metadata_json,
                ),
            )

    def search_dataset_entries(
        self,
        session_id: Optional[str],
        user_id: Optional[str] = None,
        item_name: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []

        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)

        if not clauses:
            return []

        where_scope = " OR ".join(clauses)
        query = f"""
            SELECT dataset_name, entry_type, title, content, item_name, price, source_kind, metadata_json, created_at, updated_at
            FROM dataset_entries
            WHERE ({where_scope})
        """

        if item_name:
            query += """
                AND (
                    item_name IS NOT NULL AND LOWER(item_name) LIKE LOWER(?)
                    OR LOWER(title) LIKE LOWER(?)
                    OR LOWER(content) LIKE LOWER(?)
                )
            """
            like_value = f"%{item_name}%"
            params.extend([like_value, like_value, like_value])

        query += """
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
        """
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_dataset_entries(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT dataset_name, entry_type, title, content, item_name, price, source_kind, metadata_json, created_at, updated_at
                FROM dataset_entries
                WHERE session_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
