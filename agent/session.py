"""File-backed session persistence."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.storage import project_data_dir


SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
SNAPSHOT_EVENT_INTERVAL = 50
MAX_SESSION_ID_ATTEMPTS = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class SessionRecord:
    """Serializable conversation state for one workspace session."""

    id: str
    workspace: str
    created_at: str
    updated_at: str
    title: str = "New session"
    messages: list[dict[str, str]] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        workspace: Path,
        *,
        title: str = "New session",
        session_id: str | None = None,
    ) -> "SessionRecord":
        timestamp = _now()
        return cls(
            id=session_id or _new_session_id(),
            workspace=str(workspace),
            created_at=timestamp,
            updated_at=timestamp,
            title=title,
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "SessionRecord":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported session schema")
        messages = payload.get("messages")
        tasks = payload.get("tasks")
        turns = payload.get("turns", [])
        if not isinstance(messages, list) or not isinstance(tasks, list):
            raise ValueError("invalid session content")
        if not isinstance(turns, list):
            turns = []
        return cls(
            id=str(payload["id"]),
            workspace=str(payload["workspace"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            title=str(payload.get("title") or "New session"),
            messages=[
                {"role": str(item["role"]), "content": str(item["content"])}
                for item in messages
                if isinstance(item, dict)
                and "role" in item
                and "content" in item
            ],
            tasks=[str(task) for task in tasks],
            turns=[dict(turn) for turn in turns if isinstance(turn, dict)],
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "messages": list(self.messages),
            "tasks": list(self.tasks),
            "turns": list(self.turns),
        }

    def touch(self) -> None:
        self.updated_at = _now()


class SessionStore:
    """Persist and discover sessions for one workspace."""

    def __init__(self, workspace: Path, *, root: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.directory = project_data_dir(self.workspace, root=root) / "sessions"
        self._known: dict[str, SessionRecord] = {}
        self._event_counts: dict[str, int] = {}
        self._reserved_ids: set[str] = set()

    def draft(self, *, title: str = "New session") -> SessionRecord:
        """Create an in-memory session that is persisted on its first task."""

        return SessionRecord.new(
            self.workspace,
            title=title,
            session_id=self._reserve_session_id(),
        )

    def create(self, *, title: str = "New session") -> SessionRecord:
        session = self.draft(title=title)
        self.save(session)
        return session

    def save(self, session: SessionRecord) -> None:
        session.touch()
        directory = self._path(session.id)
        self._reserved_ids.add(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "meta.json").exists():
            self._write_meta(session, self._event_count(session.id, directory))

        previous = self._known.get(session.id)
        events = self._events_between(previous, session)
        event_count = self._event_count(session.id, directory)
        if events:
            self._append_events(directory, events)
            event_count += len(events)
            self._event_counts[session.id] = event_count
        self._write_meta(session, event_count)
        if events and self._should_write_snapshot(session.id, directory, event_count):
            self._write_snapshot(session, event_count)
        self._known[session.id] = self._clone(session)

    def load(self, session_id: str) -> SessionRecord | None:
        try:
            directory = self._path(session_id)
            meta = self._read_meta(directory / "meta.json")
            session, start = self._read_snapshot(directory / "snapshot.json")
            if session is None:
                timestamp = str(meta.get("created_at") or _now())
                session = SessionRecord(
                    id=str(meta["id"]),
                    workspace=str(meta["workspace"]),
                    created_at=timestamp,
                    updated_at=str(meta.get("updated_at") or timestamp),
                    title=str(meta.get("title") or "New session"),
                )
            self._replay_events(session, directory / "events.jsonl", start=start)
            session.title = str(meta.get("title") or session.title)
            session.updated_at = str(meta.get("updated_at") or session.updated_at)
            self._known[session.id] = self._clone(session)
            self._event_counts[session.id] = self._count_events(directory)
            self._reserved_ids.add(session.id)
            return session
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def latest(self) -> SessionRecord | None:
        sessions = self.list()
        return sessions[0] if sessions else None

    def list(self, *, limit: int | None = None) -> list[SessionRecord]:
        try:
            paths = [path for path in self.directory.iterdir() if path.is_dir()]
        except OSError:
            return []
        sessions = [
            session
            for path in paths
            if (session := self._load_path(path)) and session.tasks
        ]
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions[:limit] if limit is not None else sessions

    def _path(self, session_id: str) -> Path:
        safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
        if not safe_id:
            raise ValueError("session id cannot be empty")
        return self.directory / safe_id

    def _load_path(self, path: Path) -> SessionRecord | None:
        return self.load(path.name)

    def _reserve_session_id(self) -> str:
        for _attempt in range(MAX_SESSION_ID_ATTEMPTS):
            session_id = _new_session_id()
            if session_id in self._reserved_ids:
                continue
            if self._path(session_id).exists():
                continue
            self._reserved_ids.add(session_id)
            return session_id
        raise RuntimeError("could not generate a unique session id")

    def _read_meta(self, path: Path) -> dict[str, Any]:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported session meta schema")
        return meta

    def _write_meta(self, session: SessionRecord, event_count: int) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "id": session.id,
            "workspace": session.workspace,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "title": session.title,
            "task_count": len(session.tasks),
            "event_count": event_count,
        }
        self._write_json(self._path(session.id) / "meta.json", payload)

    def _read_snapshot(self, path: Path) -> tuple[SessionRecord | None, int]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                return None, 0
            event_count = int(payload.get("event_count", 0))
            session = SessionRecord.from_json(payload["session"])
            return session, max(0, event_count)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, 0

    def _write_snapshot(self, session: SessionRecord, event_count: int) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_count": event_count,
            "session": session.as_json(),
        }
        self._write_json(self._path(session.id) / "snapshot.json", payload)

    def _should_write_snapshot(
        self,
        session_id: str,
        directory: Path,
        event_count: int,
    ) -> bool:
        _session, snapshot_event_count = self._read_snapshot(
            directory / "snapshot.json"
        )
        if event_count == 0:
            return False
        if snapshot_event_count == 0:
            return event_count >= SNAPSHOT_EVENT_INTERVAL
        return event_count - snapshot_event_count >= SNAPSHOT_EVENT_INTERVAL

    def _append_events(
        self,
        directory: Path,
        events: list[dict[str, Any]],
    ) -> None:
        path = directory / "events.jsonl"
        with path.open("a", encoding="utf-8") as file:
            for event in events:
                file.write(
                    json.dumps(event, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )

    def _replay_events(
        self,
        session: SessionRecord,
        path: Path,
        *,
        start: int = 0,
    ) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for index, line in enumerate(lines, 1):
            if index <= start or not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break
            if isinstance(event, dict):
                self._apply_event(session, event)

    def _apply_event(self, session: SessionRecord, event: dict[str, Any]) -> None:
        if event.get("schema_version") != EVENT_SCHEMA_VERSION:
            return
        event_type = event.get("type")
        if event_type == "message_appended":
            message = event.get("message")
            if isinstance(message, dict) and "role" in message and "content" in message:
                session.messages.append(
                    {
                        "role": str(message["role"]),
                        "content": str(message["content"]),
                    }
                )
        elif event_type == "messages_replaced":
            messages = event.get("messages")
            if isinstance(messages, list):
                session.messages = [
                    {"role": str(item["role"]), "content": str(item["content"])}
                    for item in messages
                    if isinstance(item, dict)
                    and "role" in item
                    and "content" in item
                ]
        elif event_type == "task_submitted":
            task = str(event.get("task", ""))
            if task:
                session.tasks.append(task)
            turn = event.get("turn")
            if isinstance(turn, dict):
                session.turns.append(dict(turn))
            elif task:
                session.turns.append({"task": task, "tools": []})
        elif event_type == "tasks_replaced":
            tasks = event.get("tasks")
            if isinstance(tasks, list):
                session.tasks = [str(task) for task in tasks]
        elif event_type == "turn_updated":
            index = event.get("index")
            turn = event.get("turn")
            if isinstance(index, int) and index >= 0 and isinstance(turn, dict):
                while len(session.turns) <= index:
                    session.turns.append({})
                session.turns[index] = dict(turn)
        elif event_type == "turns_replaced":
            turns = event.get("turns")
            if isinstance(turns, list):
                session.turns = [
                    dict(turn) for turn in turns if isinstance(turn, dict)
                ]

    def _events_between(
        self,
        previous: SessionRecord | None,
        current: SessionRecord,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        previous_messages = previous.messages if previous is not None else []
        previous_tasks = previous.tasks if previous is not None else []
        previous_turns = previous.turns if previous is not None else []

        if self._is_prefix(previous_tasks, current.tasks):
            for index in range(len(previous_tasks), len(current.tasks)):
                turn = (
                    current.turns[index]
                    if index < len(current.turns)
                    else {"task": current.tasks[index], "tools": []}
                )
                events.append(
                    self._event(
                        "task_submitted",
                        task=current.tasks[index],
                        turn=dict(turn),
                    )
                )
            existing_turns = min(len(previous_turns), len(current.turns))
            for index in range(existing_turns):
                if previous_turns[index] != current.turns[index]:
                    events.append(
                        self._event(
                            "turn_updated",
                            index=index,
                            turn=dict(current.turns[index]),
                        )
                    )
        else:
            events.append(self._event("tasks_replaced", tasks=list(current.tasks)))
            events.append(
                self._event(
                    "turns_replaced",
                    turns=[dict(turn) for turn in current.turns],
                )
            )

        if self._is_prefix(previous_messages, current.messages):
            for message in current.messages[len(previous_messages):]:
                events.append(
                    self._event(
                        "message_appended",
                        message=dict(message),
                    )
                )
        else:
            events.append(
                self._event(
                    "messages_replaced",
                    messages=[dict(message) for message in current.messages],
                )
            )

        return events

    @staticmethod
    def _event(event_type: str, **payload: Any) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "type": event_type,
            "timestamp": _now(),
            **payload,
        }

    def _event_count(self, session_id: str, directory: Path) -> int:
        if session_id not in self._event_counts:
            self._event_counts[session_id] = self._count_events(directory)
        return self._event_counts[session_id]

    @staticmethod
    def _count_events(directory: Path) -> int:
        try:
            with (directory / "events.jsonl").open(encoding="utf-8") as file:
                return sum(1 for line in file if line.strip())
        except FileNotFoundError:
            return 0

    @staticmethod
    def _is_prefix(prefix: list[Any], full: list[Any]) -> bool:
        return len(prefix) <= len(full) and full[: len(prefix)] == prefix

    @staticmethod
    def _clone(session: SessionRecord) -> SessionRecord:
        return SessionRecord.from_json(deepcopy(session.as_json()))

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
