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

    def test_telegram_update_offset_can_be_persisted(self) -> None:
        self.assertIsNone(self.storage.get_telegram_update_offset())

        self.storage.set_telegram_update_offset(1234)

        self.assertEqual(self.storage.get_telegram_update_offset(), 1234)

    def test_processed_message_can_be_marked_and_queried(self) -> None:
        self.assertFalse(self.storage.has_processed_message(100, 200))

        self.storage.mark_message_processed(
            chat_id=100,
            message_id=200,
            user_id=1,
            message_thread_id=9,
            update_id=300,
            text="测试消息",
        )

        self.assertTrue(self.storage.has_processed_message(100, 200))

    def test_pending_telegram_message_can_be_enqueued_rescheduled_and_deleted(self) -> None:
        due_at = "2026-04-04T13:30:00+00:00"
        pending_id = self.storage.enqueue_pending_telegram_message(
            user_id=1,
            chat_id=100,
            text="待发送消息",
            message_thread_id=9,
            reply_to_message_id=88,
            parse_mode="HTML",
            due_at=due_at,
        )

        due_messages = self.storage.list_due_pending_telegram_messages(
            due_before="2026-04-04T13:31:00+00:00",
            limit=10,
        )

        self.assertEqual(len(due_messages), 1)
        self.assertEqual(due_messages[0].id, pending_id)
        self.assertEqual(due_messages[0].user_id, 1)
        self.assertEqual(due_messages[0].message_thread_id, 9)
        self.assertEqual(due_messages[0].reply_to_message_id, 88)
        self.assertEqual(self.storage.get_next_pending_telegram_message_due_at(), due_at)

        rescheduled_due_at = "2026-04-04T14:00:00+00:00"
        self.storage.reschedule_pending_telegram_message(pending_id, rescheduled_due_at)
        self.assertEqual(
            self.storage.get_next_pending_telegram_message_due_at(),
            rescheduled_due_at,
        )

        self.storage.delete_pending_telegram_message(pending_id)
        self.assertIsNone(self.storage.get_next_pending_telegram_message_due_at())

    def test_reconcile_interrupted_runs_marks_running_runs_stopped(self) -> None:
        self.storage.ensure_user(11, chat_id=104, message_thread_id=8)
        session = self.storage.create_session(11, "会话")
        self.storage.update_session_status(session.id, "running")
        run_id = self.storage.create_run(session.id, 11, "长任务")

        recovered_sessions = self.storage.reconcile_interrupted_runs("服务重启，已回收未完成任务")

        self.assertEqual(recovered_sessions, 1)
        refreshed = self.storage.get_session(session.id, 11)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "idle")
        row = self.storage._conn.execute(
            "SELECT status, ended_at, error_message FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "stopped")
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["error_message"], "服务重启，已回收未完成任务")


if __name__ == "__main__":
    unittest.main()
