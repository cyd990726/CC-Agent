import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.memory import MemoryStore
from agent.session import SNAPSHOT_EVENT_INTERVAL, SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_save_and_load_session(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        session = store.create(title="Fix tests")
        session.messages = [{"role": "user", "content": "hello"}]
        session.tasks = ["hello"]
        session.turns = [
            {"task": "hello", "answer": "done", "tools": []}
        ]

        store.save(session)
        loaded = store.load(session.id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.title, "Fix tests")
        self.assertEqual(loaded.messages, session.messages)
        self.assertEqual(loaded.tasks, ["hello"])
        self.assertEqual(loaded.turns, session.turns)
        session_dir = store.directory / session.id
        self.assertTrue(session_dir.joinpath("meta.json").exists())
        self.assertTrue(session_dir.joinpath("events.jsonl").exists())

    def test_save_appends_incremental_events(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        session = store.draft()
        session.tasks = ["inspect"]
        session.turns = [{"task": "inspect", "tools": [], "status": "queued"}]
        session.messages = [{"role": "user", "content": "inspect"}]

        store.save(session)
        events_path = store.directory / session.id / "events.jsonl"
        first_events = events_path.read_text(encoding="utf-8").splitlines()
        session.messages.append(
            {
                "role": "user",
                "content": 'Observation:\n{"output": "README.md"}',
            }
        )
        store.save(session)
        second_events = events_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(first_events), 2)
        self.assertEqual(len(second_events), 3)
        self.assertEqual(
            json.loads(second_events[-1])["type"],
            "message_appended",
        )

    def test_list_skips_corrupt_sessions(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        valid = store.create(title="Valid")
        valid.tasks = ["valid task"]
        store.save(valid)
        bad = store.directory / "bad"
        bad.mkdir(parents=True)
        bad.joinpath("meta.json").write_text("{", encoding="utf-8")

        sessions = store.list()

        self.assertEqual([session.id for session in sessions], [valid.id])

    def test_draft_and_empty_session_are_not_listed(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        draft = store.draft()
        empty = store.create()

        self.assertFalse(store.directory.joinpath(draft.id).exists())
        self.assertEqual(store.list(), [])
        self.assertIsNotNone(store.load(empty.id))

    def test_draft_session_id_skips_existing_session_directory(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        existing = store.directory / "aaaaaaaaaaaa"
        existing.mkdir(parents=True)

        with patch(
            "agent.session._new_session_id",
            side_effect=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        ):
            session = store.draft()

        self.assertEqual(session.id, "bbbbbbbbbbbb")
        self.assertFalse(store.directory.joinpath(session.id).exists())

    def test_draft_session_id_skips_reserved_draft_id(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")

        with patch(
            "agent.session._new_session_id",
            side_effect=["aaaaaaaaaaaa", "aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        ):
            first = store.draft()
            second = store.draft()

        self.assertEqual(first.id, "aaaaaaaaaaaa")
        self.assertEqual(second.id, "bbbbbbbbbbbb")

    def test_snapshot_accelerates_replay_but_events_remain_source(self) -> None:
        store = SessionStore(self.workspace, root=self.root / "data")
        session = store.draft()
        for index in range(SNAPSHOT_EVENT_INTERVAL):
            session.messages.append(
                {"role": "user", "content": f"message {index}"}
            )
            store.save(session)

        session_dir = store.directory / session.id
        snapshot = json.loads(
            session_dir.joinpath("snapshot.json").read_text(encoding="utf-8")
        )
        events_path = session_dir / "events.jsonl"
        events_path.write_text(
            events_path.read_text(encoding="utf-8")
            + json.dumps(
                {
                    "schema_version": 1,
                    "type": "message_appended",
                    "message": {"role": "user", "content": "after snapshot"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        reloaded = SessionStore(self.workspace, root=self.root / "data").load(
            session.id
        )

        self.assertEqual(snapshot["event_count"], SNAPSHOT_EVENT_INTERVAL)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.messages[-1]["content"], "after snapshot")


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_append_and_prompt_section_include_memory(self) -> None:
        memory = MemoryStore(self.workspace, root=self.root / "data")

        memory.append("Prefer concise terminal answers.")

        content = memory.read()
        self.assertIn("Prefer concise terminal answers.", content)
        self.assertIn(str(memory.directory), memory.prompt_section())
        self.assertIn("Prefer concise terminal answers.", memory.prompt_section())
        self.assertIn("Use it proactively", memory.prompt_section())
        self.assertIn("do not ask for separate confirmation", memory.prompt_section())


if __name__ == "__main__":
    unittest.main()
