from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class UserState:
    user_id: int
    active_session_id: int | None
    last_chat_id: int | None
    last_message_thread_id: int | None
    updated_at: str


@dataclass(slots=True)
class SessionRecord:
    id: int
    user_id: int
    title: str
    codex_thread_id: str | None
    bound_path: str | None
    status: str
    created_at: str
    updated_at: str
    last_active_at: str


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    active_session_id INTEGER,
                    last_chat_id INTEGER,
                    last_message_thread_id INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    codex_thread_id TEXT,
                    bound_path TEXT,
                    status TEXT NOT NULL DEFAULT 'idle',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    preview_chat_id INTEGER,
                    preview_message_id INTEGER,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    error_message TEXT,
                    final_excerpt TEXT
                );

                CREATE TABLE IF NOT EXISTS session_contexts (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER NOT NULL DEFAULT 0,
                    active_session_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, chat_id, message_thread_id)
                );
                """
            )
            self._conn.execute(
                """
                UPDATE sessions
                SET title = bound_path
                WHERE bound_path IS NOT NULL
                  AND TRIM(bound_path) != ''
                  AND title != bound_path
                """
            )
            self._conn.commit()

    def ensure_user(
        self, user_id: int, chat_id: int | None = None, message_thread_id: int | None = None
    ) -> UserState:
        now = _utc_now()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT user_id, active_session_id, last_chat_id, last_message_thread_id, updated_at
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO users (
                        user_id, active_session_id, last_chat_id, last_message_thread_id, updated_at
                    ) VALUES (?, NULL, ?, ?, ?)
                    """,
                    (user_id, chat_id, message_thread_id, now),
                )
                self._conn.commit()
                return UserState(
                    user_id=user_id,
                    active_session_id=None,
                    last_chat_id=chat_id,
                    last_message_thread_id=message_thread_id,
                    updated_at=now,
                )

            self._conn.execute(
                """
                UPDATE users
                SET last_chat_id = ?, last_message_thread_id = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    chat_id if chat_id is not None else existing["last_chat_id"],
                    (
                        message_thread_id
                        if message_thread_id is not None
                        else existing["last_message_thread_id"]
                    ),
                    now,
                    user_id,
                ),
            )
            self._conn.commit()
            return self.get_user(user_id)

    def get_user(self, user_id: int) -> UserState | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT user_id, active_session_id, last_chat_id, last_message_thread_id, updated_at
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_user(row)

    def create_session(self, user_id: int, title: str) -> SessionRecord:
        now = _utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    user_id, title, codex_thread_id, bound_path, status,
                    created_at, updated_at, last_active_at
                ) VALUES (?, ?, NULL, NULL, 'idle', ?, ?, ?)
                """,
                (user_id, title, now, now, now),
            )
            session_id = int(cursor.lastrowid)
            self._conn.execute(
                """
                INSERT INTO users (
                    user_id, active_session_id, last_chat_id, last_message_thread_id, updated_at
                ) VALUES (?, ?, NULL, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    active_session_id = excluded.active_session_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, session_id, now),
            )
            self._conn.commit()
        session = self.get_session(session_id, user_id)
        assert session is not None
        return session

    def list_sessions(self, user_id: int) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, user_id, title, codex_thread_id, bound_path, status,
                       created_at, updated_at, last_active_at
                FROM sessions
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def get_session(self, session_id: int, user_id: int | None = None) -> SessionRecord | None:
        query = """
            SELECT id, user_id, title, codex_thread_id, bound_path, status,
                   created_at, updated_at, last_active_at
            FROM sessions
            WHERE id = ?
        """
        params: list[Any] = [session_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return self._row_to_session(row)

    def get_active_session(
        self,
        user_id: int,
        chat_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SessionRecord | None:
        if chat_id is not None:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT active_session_id
                    FROM session_contexts
                    WHERE user_id = ? AND chat_id = ? AND message_thread_id = ?
                    """,
                    (user_id, chat_id, self._normalize_message_thread_id(message_thread_id)),
                ).fetchone()
            if row is not None and row["active_session_id"] is not None:
                return self.get_session(int(row["active_session_id"]), user_id)

        user = self.get_user(user_id)
        if user is None or user.active_session_id is None:
            return None
        return self.get_session(user.active_session_id, user_id)

    def set_active_session(
        self,
        user_id: int,
        session_id: int,
        chat_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE users
                SET active_session_id = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (session_id, now, user_id),
            )
            self._conn.execute(
                """
                UPDATE sessions
                SET last_active_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, now, session_id, user_id),
            )
            if chat_id is not None:
                self._conn.execute(
                    """
                    INSERT INTO session_contexts (
                        user_id, chat_id, message_thread_id, active_session_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, chat_id, message_thread_id) DO UPDATE SET
                        active_session_id = excluded.active_session_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        chat_id,
                        self._normalize_message_thread_id(message_thread_id),
                        session_id,
                        now,
                    ),
                )
            self._conn.commit()

    def bind_session_path(self, session_id: int, user_id: int, bound_path: str) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE sessions
                SET title = ?, bound_path = ?, codex_thread_id = NULL, updated_at = ?, last_active_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (bound_path, bound_path, now, now, session_id, user_id),
            )
            self._conn.commit()

    def update_session_thread_id(self, session_id: int, thread_id: str) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE sessions
                SET codex_thread_id = ?, updated_at = ?, last_active_at = ?
                WHERE id = ?
                """,
                (thread_id, now, now, session_id),
            )
            self._conn.commit()

    def update_session_status(self, session_id: int, status: str) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?, last_active_at = ?
                WHERE id = ?
                """,
                (status, now, now, session_id),
            )
            self._conn.commit()

    def create_run(
        self,
        session_id: int,
        user_id: int,
        prompt: str,
        preview_chat_id: int | None = None,
        preview_message_id: int | None = None,
    ) -> int:
        now = _utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO runs (
                    session_id, user_id, prompt, status, preview_chat_id,
                    preview_message_id, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (session_id, user_id, prompt, preview_chat_id, preview_message_id, now),
            )
            run_id = int(cursor.lastrowid)
            self._conn.commit()
        return run_id

    def set_run_preview_message(
        self, run_id: int, preview_chat_id: int, preview_message_id: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET preview_chat_id = ?, preview_message_id = ?
                WHERE id = ?
                """,
                (preview_chat_id, preview_message_id, run_id),
            )
            self._conn.commit()

    def finish_run(
        self, run_id: int, status: str, final_excerpt: str | None = None, error_message: str | None = None
    ) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, ended_at = ?, final_excerpt = ?, error_message = ?
                WHERE id = ?
                """,
                (status, now, final_excerpt, error_message, run_id),
            )
            self._conn.commit()

    def _row_to_user(self, row: sqlite3.Row | None) -> UserState | None:
        if row is None:
            return None
        return UserState(
            user_id=int(row["user_id"]),
            active_session_id=(
                int(row["active_session_id"]) if row["active_session_id"] is not None else None
            ),
            last_chat_id=int(row["last_chat_id"]) if row["last_chat_id"] is not None else None,
            last_message_thread_id=(
                int(row["last_message_thread_id"])
                if row["last_message_thread_id"] is not None
                else None
            ),
            updated_at=str(row["updated_at"]),
        )

    def _normalize_message_thread_id(self, message_thread_id: int | None) -> int:
        return int(message_thread_id) if message_thread_id is not None else 0

    def _row_to_session(self, row: sqlite3.Row | None) -> SessionRecord | None:
        if row is None:
            return None
        return SessionRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            title=str(row["title"]),
            codex_thread_id=str(row["codex_thread_id"]) if row["codex_thread_id"] else None,
            bound_path=str(row["bound_path"]) if row["bound_path"] else None,
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_active_at=str(row["last_active_at"]),
        )
