from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from teledex.storage import Storage


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.storage = Storage(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_session_and_switch_active(self) -> None:
        self.storage.ensure_user(1, chat_id=100, message_thread_id=None)
        session_a = self.storage.create_session(1, "会话 A")
        session_b = self.storage.create_session(1, "会话 B")

        active = self.storage.get_active_session(1)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, session_b.id)

        self.storage.set_active_session(1, session_a.id)
        active = self.storage.get_active_session(1)
        assert active is not None
        self.assertEqual(active.id, session_a.id)

    def test_active_session_can_be_scoped_by_message_thread(self) -> None:
        self.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session_a = self.storage.create_session(1, "会话 A")
        session_b = self.storage.create_session(1, "会话 B")

        self.storage.set_active_session(1, session_a.id, chat_id=100, message_thread_id=9)
        self.storage.set_active_session(1, session_b.id, chat_id=100, message_thread_id=10)

        active_a = self.storage.get_active_session(1, chat_id=100, message_thread_id=9)
        active_b = self.storage.get_active_session(1, chat_id=100, message_thread_id=10)

        assert active_a is not None
        assert active_b is not None
        self.assertEqual(active_a.id, session_a.id)
        self.assertEqual(active_b.id, session_b.id)

    def test_bind_path_and_thread_id(self) -> None:
        self.storage.ensure_user(2, chat_id=101, message_thread_id=None)
        session = self.storage.create_session(2, "会话")
        self.storage.bind_session_path(session.id, 2, "/root/demo")
        self.storage.update_session_thread_id(session.id, "thread-123")
        self.storage.bind_session_path(session.id, 2, "/root/demo-next")

        fetched = self.storage.get_session(session.id, 2)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.title, "demo-next")
        self.assertEqual(fetched.bound_path, "/root/demo-next")
        self.assertIsNone(fetched.codex_thread_id)

    def test_get_session_by_bound_path_returns_matching_session(self) -> None:
        self.storage.ensure_user(2, chat_id=101, message_thread_id=None)
        session = self.storage.create_session(2, "会话")
        self.storage.bind_session_path(session.id, 2, "/root/demo")

        fetched = self.storage.get_session_by_bound_path(2, "/root/demo")

        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.id, session.id)
        self.assertEqual(fetched.title, "demo")


if __name__ == "__main__":
    unittest.main()
