import itertools
import json
import unittest
from io import StringIO
from pathlib import Path

from rich.console import Console

from agent.events import AgentEvent, EventType
from tools.base import ToolResult
from ui.presentation import (
    JsonStringFieldStreamer,
    PresentationAdapter,
    PresentationStore,
    TextDeltaBuffer,
    ToolPresenterRegistry,
)
from ui.renderer import TerminalRenderer
from ui.view_model import (
    AssistantTextDelta,
    BlockStatus,
    ContentArrived,
    EventContext,
    FollowTail,
    PermissionChoiceViewModel,
    PermissionRequestViewModel,
    PermissionRequested,
    RunStarted,
    ScrollAway,
    SelectPermission,
    SessionPresentation,
    ShowPermission,
    TerminalViewState,
    TextBlockViewModel,
    ToggleBlock,
    ToolBlockViewModel,
    ToolRequested,
    ToolRequestView,
    ToolStarted,
    TurnPhase,
    reduce_presentation,
    reduce_terminal,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    def __init__(self) -> None:
        self.wall_ms = 1_700_000_000_000
        self.monotonic_ms = 10_000

    def wall(self) -> int:
        return self.wall_ms

    def monotonic(self) -> int:
        return self.monotonic_ms

    def advance(self, milliseconds: int = 10) -> None:
        self.wall_ms += milliseconds
        self.monotonic_ms += milliseconds


def event_context(sequence: int, *, turn_id: str = "turn-1") -> EventContext:
    return EventContext("session-1", turn_id, sequence, 1000 + sequence)


class PresentationReducerTests(unittest.TestCase):
    def test_two_tools_keep_independent_lifecycle_state(self) -> None:
        state = SessionPresentation.empty("session-1")
        state = reduce_presentation(
            state,
            RunStarted(event_context(1), "inspect files"),
        )
        state = reduce_presentation(
            state,
            ToolRequested(
                event_context(2),
                "call-1",
                "read_file",
                ToolRequestView("file_read", "a.py"),
            ),
        )
        state = reduce_presentation(
            state,
            ToolRequested(
                event_context(3),
                "call-2",
                "read_file",
                ToolRequestView("file_read", "b.py"),
            ),
        )
        state = reduce_presentation(
            state,
            ToolStarted(event_context(4), "call-1"),
        )
        state = reduce_presentation(
            state,
            ToolStarted(event_context(5), "call-2"),
        )

        tools = [
            block
            for block in state.turns[0].blocks
            if isinstance(block, ToolBlockViewModel)
        ]
        self.assertEqual([tool.call_id for tool in tools], ["call-1", "call-2"])
        self.assertTrue(all(tool.status is BlockStatus.RUNNING for tool in tools))
        self.assertEqual(state.turns[0].phase, TurnPhase.RUNNING_TOOLS)

    def test_duplicate_or_foreign_events_do_not_change_state(self) -> None:
        state = SessionPresentation.empty("session-1")
        started = RunStarted(event_context(1), "task")
        state = reduce_presentation(state, started)

        self.assertIs(reduce_presentation(state, started), state)
        foreign = RunStarted(
            EventContext("another-session", "turn-2", 2, 1002),
            "other",
        )
        self.assertIs(reduce_presentation(state, foreign), state)

    def test_permission_focus_is_client_local(self) -> None:
        presentation = SessionPresentation.empty("session-1")
        presentation = reduce_presentation(
            presentation,
            RunStarted(event_context(1), "edit a file"),
        )
        presentation = reduce_presentation(
            presentation,
            ToolRequested(
                event_context(2),
                "call-1",
                "edit_file",
                ToolRequestView("file_edit", "a.py"),
            ),
        )
        request = PermissionRequestViewModel(
            request_id="permission-1",
            call_id="call-1",
            kind="diff",
            title="Allow this edit?",
            risk=None,
            preview=("- old", "+ new"),
            choices=(
                PermissionChoiceViewModel("once", "Allow once", "One call"),
                PermissionChoiceViewModel("deny", "Deny", "Do not run"),
            ),
        )
        presentation = reduce_presentation(
            presentation,
            PermissionRequested(event_context(3), request),
        )
        terminal = reduce_terminal(
            TerminalViewState(),
            ShowPermission("permission-1"),
        )
        terminal = reduce_terminal(terminal, SelectPermission(1))

        self.assertEqual(
            presentation.turns[0].phase,
            TurnPhase.WAITING_PERMISSION,
        )
        self.assertEqual(presentation.permission_requests, (request,))
        self.assertEqual(terminal.active_permission_id, "permission-1")
        self.assertEqual(terminal.selected_permission_index, 1)
        restored = SessionPresentation.from_dict(
            json.loads(json.dumps(presentation.to_dict(), ensure_ascii=False))
        )
        self.assertEqual(restored, presentation)

    def test_terminal_expansion_and_unseen_state_are_local(self) -> None:
        original = TerminalViewState()
        browsing = reduce_terminal(original, ScrollAway("block-1"))
        browsing = reduce_terminal(browsing, ContentArrived(2))
        expanded = reduce_terminal(browsing, ToggleBlock("block-1"))

        self.assertTrue(original.follow_tail)
        self.assertFalse(expanded.follow_tail)
        self.assertEqual(expanded.unseen_count, 2)
        self.assertEqual(expanded.expanded_block_ids, frozenset({"block-1"}))

        following = reduce_terminal(expanded, FollowTail())
        self.assertTrue(following.follow_tail)
        self.assertEqual(following.unseen_count, 0)
        self.assertIsNone(following.anchor_block_id)


class PresentationAdapterTests(unittest.TestCase):
    def make_adapter(self):
        clock = FakeClock()
        identifiers = (str(value) for value in itertools.count(1))
        adapter = PresentationAdapter(
            session_id="session-1",
            id_factory=lambda: next(identifiers),
            wall_clock_ms=clock.wall,
            monotonic_ms=clock.monotonic,
            frame_interval_ms=33,
        )
        return adapter, clock

    def test_action_observation_trace_builds_serializable_snapshot(self) -> None:
        adapter, clock = self.make_adapter()
        store = PresentationStore(adapter=adapter)
        raw_events = json.loads(
            (FIXTURES / "presentation_action_observation.json").read_text(
                encoding="utf-8"
            )
        )

        for item in raw_events:
            store.handle(AgentEvent(EventType(item["type"]), item["data"]))
            clock.advance()

        state = store.state
        self.assertIsNone(state.active_turn_id)
        self.assertEqual(len(state.turns), 1)
        turn = state.turns[0]
        self.assertEqual(turn.phase, TurnPhase.COMPLETED)
        self.assertEqual(turn.prompt, "inspect the login flow")
        self.assertEqual(turn.step, 2)

        commentary = [
            block
            for block in turn.blocks
            if isinstance(block, TextBlockViewModel)
            and block.role == "commentary"
        ]
        tools = [
            block
            for block in turn.blocks
            if isinstance(block, ToolBlockViewModel)
        ]
        answers = [
            block
            for block in turn.blocks
            if isinstance(block, TextBlockViewModel)
            and block.role == "assistant"
        ]
        self.assertEqual(
            commentary[0].text,
            "I will inspect the authentication module.",
        )
        self.assertEqual(tools[0].call_id, "call:3")
        self.assertEqual(tools[0].status, BlockStatus.SUCCEEDED)
        self.assertEqual(tools[0].summary, "Read lines 1-2 of 2")
        self.assertEqual(answers[0].text, "Fixed the issue.")
        self.assertFalse(answers[0].streaming)

        encoded = json.dumps(state.to_dict(), ensure_ascii=False)
        restored = SessionPresentation.from_dict(json.loads(encoded))
        self.assertEqual(restored, state)

    def test_queue_and_permission_lifecycle_share_stable_ids(self) -> None:
        adapter, _clock = self.make_adapter()
        store = PresentationStore(adapter=adapter)
        turn_id = store.queue_task("edit a file")

        self.assertEqual(store.state.queued_turn_ids, (turn_id,))
        store.handle(AgentEvent(EventType.RUN_STARTED, {"task": "edit a file"}))
        self.assertEqual(store.state.active_turn_id, turn_id)
        self.assertEqual(store.state.queued_turn_ids, ())

        store.handle(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {
                    "tool": "edit_file",
                    "args": {
                        "path": "a.py",
                        "old_text": "old",
                        "new_text": "new",
                    },
                },
            )
        )
        store.handle(
            AgentEvent(EventType.TOOL_STARTED, {"tool": "edit_file"})
        )
        request = store.request_permission(
            "edit_file",
            {"path": "a.py"},
            (
                PermissionChoiceViewModel("y", "Allow once", "One call"),
                PermissionChoiceViewModel("n", "Deny", "Do not run"),
            ),
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(len(store.state.permission_requests), 1)
        self.assertEqual(store.state.turns[0].phase, TurnPhase.WAITING_PERMISSION)

        store.resolve_permission(request.request_id, request.call_id, allowed=True)

        self.assertEqual(store.state.permission_requests, ())
        self.assertEqual(store.state.turns[0].phase, TurnPhase.RUNNING_TOOLS)

    def test_model_delta_is_batched_and_flushed_before_completion(self) -> None:
        adapter, clock = self.make_adapter()
        adapter.translate(AgentEvent(EventType.RUN_STARTED, {"task": "task"}))
        adapter.translate(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))

        first = adapter.translate(
            AgentEvent(EventType.MODEL_DELTA, {"delta": '{"final_answer":"a'})
        )
        clock.advance(10)
        second = adapter.translate(
            AgentEvent(EventType.MODEL_DELTA, {"delta": 'b'})
        )
        completed = adapter.translate(
            AgentEvent(
                EventType.MODEL_COMPLETED,
                {"step": 1, "response": {"final_answer": "ab"}},
            )
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].text, "a")
        self.assertEqual(second, ())
        self.assertEqual(len(completed), 1)
        self.assertIsInstance(completed[0], AssistantTextDelta)
        self.assertEqual(completed[0].text, "b")

    def test_json_streamer_handles_unicode_across_every_boundary(self) -> None:
        answer = "中文 😀 done\n"
        payload = json.dumps({"final_answer": answer}, ensure_ascii=True)
        for split in range(len(payload) + 1):
            stream = JsonStringFieldStreamer("final_answer")
            result = stream.feed(payload[:split]) + stream.feed(payload[split:])
            self.assertEqual(result, answer)

    def test_cancel_flushes_buffered_answer_and_closes_turn(self) -> None:
        adapter, clock = self.make_adapter()
        store = PresentationStore(adapter=adapter)
        store.handle(AgentEvent(EventType.RUN_STARTED, {"task": "task"}))
        store.handle(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        store.handle(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"partial'},
            )
        )
        clock.advance(10)
        store.handle(
            AgentEvent(EventType.MODEL_DELTA, {"delta": " answer"})
        )

        store.handle(AgentEvent(EventType.RUN_CANCELLED, {}))

        turn = store.state.turns[0]
        answers = [
            block
            for block in turn.blocks
            if isinstance(block, TextBlockViewModel)
            and block.role == "assistant"
        ]
        self.assertEqual(turn.phase, TurnPhase.CANCELLED)
        self.assertEqual(answers[0].text, "partial answer")
        self.assertFalse(answers[0].streaming)

    def test_legacy_renderer_builds_shadow_presentation_state(self) -> None:
        renderer = TerminalRenderer(Console(file=StringIO(), force_terminal=False))

        renderer(AgentEvent(EventType.RUN_STARTED, {"task": "say hello"}))
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "hello", "steps": 1},
            )
        )

        state = renderer.presentation_store.state
        self.assertEqual(state.turns[0].phase, TurnPhase.COMPLETED)
        answers = [
            block
            for block in state.turns[0].blocks
            if isinstance(block, TextBlockViewModel)
            and block.role == "assistant"
        ]
        self.assertEqual(answers[0].text, "hello")


class PresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.presenters = ToolPresenterRegistry(output_lines=3)

    def test_edit_result_exposes_summary_and_structured_metrics(self) -> None:
        result = ToolResult(
            "edit_file",
            {"path": "a.py"},
            True,
            "updated a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
        )

        presented = self.presenters.result(result)

        self.assertEqual(presented.summary, "Updated a.py · +1 / -1 lines")
        self.assertEqual(
            dict(presented.metrics),
            {"added_lines": 1, "removed_lines": 1},
        )
        self.assertEqual(presented.preview, ())
        self.assertTrue(presented.truncated)

        expanded = self.presenters.result(result, expanded=True)
        self.assertIn("@@ -1 +1 @@", expanded.preview)
        self.assertFalse(expanded.truncated)

    def test_shell_result_exposes_exit_code(self) -> None:
        result = ToolResult(
            "shell",
            {"command": "python -m unittest"},
            True,
            json.dumps(
                {
                    "exit_code": 0,
                    "stdout": "12 tests passed",
                    "stderr": "",
                }
            ),
        )

        request = self.presenters.request(result.tool, result.args)
        presented = self.presenters.result(result)

        self.assertEqual(request.kind, "shell")
        self.assertEqual(request.subject, "python -m unittest")
        self.assertEqual(presented.summary, "Exited with code 0")
        self.assertEqual(dict(presented.metrics), {"exit_code": 0})

    def test_default_preview_collapses_and_expansion_preserves_output(self) -> None:
        presenters = ToolPresenterRegistry()
        output = "\n".join(f"line {index}" for index in range(20))
        result = ToolResult("custom", {}, True, output)
        compact = presenters.result(result)
        self.assertEqual(compact.preview[:3], ("line 0", "line 1", "line 2"))
        self.assertEqual(len(compact.preview), 4)
        self.assertTrue(compact.truncated)
        self.assertEqual(presenters.result(result, expanded=True).preview, tuple(output.splitlines()))

    def test_shell_failure_previews_stderr_before_long_stdout(self) -> None:
        result = ToolResult("shell", {}, False, json.dumps({
            "exit_code": 1,
            "stdout": "\n".join(f"log {index}" for index in range(20)),
            "stderr": "Error: missing configuration",
        }))
        compact = ToolPresenterRegistry().result(result)
        self.assertEqual(compact.preview[0], "Error: missing configuration")
        self.assertEqual(compact.summary, "Exited with code 1")
        self.assertTrue(compact.truncated)

    def test_unknown_tool_has_generic_fallback(self) -> None:
        request = self.presenters.request("custom", {"value": 3})
        result = self.presenters.result(ToolResult("custom", {}, True, "ok"))

        self.assertEqual(request.kind, "generic")
        self.assertEqual(request.details, ("value: 3",))
        self.assertEqual(result.summary, "Done")
        self.assertEqual(result.preview, ("ok",))

    def test_preview_reports_when_full_output_is_not_embedded(self) -> None:
        result = self.presenters.result(
            ToolResult("custom", {}, True, "one\ntwo\nthree\nfour")
        )

        self.assertEqual(result.preview[:3], ("one", "two", "three"))
        self.assertEqual(
            result.preview[-1],
            "… 1 more line · Ctrl+O / /verbose to expand",
        )
        self.assertTrue(result.truncated)


class TextDeltaBufferTests(unittest.TestCase):
    def test_flushes_on_interval_and_newline(self) -> None:
        buffer = TextDeltaBuffer(frame_interval_ms=33)
        buffer.reset(100)

        self.assertEqual(buffer.push("a", 110), "a")
        self.assertEqual(buffer.push("b", 132), "")
        self.assertEqual(buffer.push("c", 143), "bc")
        self.assertEqual(buffer.push("next\n", 134), "next\n")
        self.assertEqual(buffer.flush(135), "")


if __name__ == "__main__":
    unittest.main()
