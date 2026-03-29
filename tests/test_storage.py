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
        self.storage.close()
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

    def test_scoped_lookup_does_not_fallback_to_user_level_active_session(self) -> None:
        self.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session_a = self.storage.create_session(1, "会话 A")
        session_b = self.storage.create_session(1, "会话 B")

        self.storage.set_active_session(1, session_a.id, chat_id=100, message_thread_id=9)
        self.storage.set_active_session(1, session_b.id)

        scoped_missing = self.storage.get_active_session(1, chat_id=100, message_thread_id=10)
        global_active = self.storage.get_active_session(1)

        self.assertIsNone(scoped_missing)
        assert global_active is not None
        self.assertEqual(global_active.id, session_b.id)

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

    def test_update_session_codex_settings_persists_json_payload(self) -> None:
        self.storage.ensure_user(3, chat_id=102, message_thread_id=None)
        session = self.storage.create_session(3, "会话")

        settings = self.storage.update_session_codex_settings(
            session.id,
            {
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "service_tier": "fast",
            },
        )

        fetched = self.storage.get_session(session.id, 3)
        self.assertEqual(
            settings,
            {
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "service_tier": "fast",
            },
        )
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.codex_settings, settings)

    def test_wipe_user_data_removes_user_sessions_runs_and_contexts(self) -> None:
        self.storage.ensure_user(9, chat_id=103, message_thread_id=7)
        session = self.storage.create_session(9, "会话")
        self.storage.bind_session_path(session.id, 9, "/root/demo")
        self.storage.set_active_session(9, session.id, chat_id=103, message_thread_id=7)
        self.storage.create_run(session.id, 9, "测试任务")

        summary = self.storage.wipe_user_data(9)

        self.assertEqual(summary.user_id, 9)
        self.assertEqual(summary.session_ids, [session.id])
        self.assertEqual(summary.bound_paths, ["/root/demo"])
        self.assertEqual(summary.sessions_deleted, 1)
        self.assertEqual(summary.runs_deleted, 1)
        self.assertEqual(summary.contexts_deleted, 1)
        self.assertTrue(summary.user_deleted)
        self.assertIsNone(self.storage.get_user(9))
        self.assertEqual(self.storage.list_sessions(9), [])


if __name__ == "__main__":
    unittest.main()
