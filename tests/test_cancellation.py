import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.cancellation import CancellationToken, RunCancelled, current_token
from agent.events import EventType
from agent.runtime import AgentRuntime
from model.llm import ChatCompletionsLLM
from tests.test_runtime import EchoTool, QueueLLM
from tools.base import ToolExecutor
from tools.shell import ShellTool


class CancellationTests(unittest.TestCase):
    def test_cancelled_model_cannot_emit_late_delta(self):
        token = CancellationToken()
        waiting = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        deltas = []
        errors = []

        def operation():
            waiting.set()
            try:
                release.wait(2)
                token.deliver(deltas.append, "stale output")
            finally:
                finished.set()

        def work():
            try:
                with token.bind():
                    token.run_io(operation)
            except RunCancelled:
                errors.append("cancelled")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.assertTrue(waiting.wait(1))
        token.cancel()
        thread.join(1)
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(deltas, [])

    def test_ui_queue_continues_after_cancelled_run(self):
        from io import StringIO
        from rich.console import Console
        from ui.renderer import TerminalRenderer
        from ui.terminal import TerminalApp

        ready = threading.Event()

        class Model(QueueLLM):
            def stream_chat(self, messages, on_delta=None):
                if messages[-1]["content"] == "first":
                    ready.set()
                    current_token.get().wait(10)
                return {"final_answer": "second finished"}

        console = Console(file=StringIO(), force_terminal=False)
        app = TerminalApp(
            AgentRuntime(Model([]), ToolExecutor([])), TerminalRenderer(console),
            console, model_name="test", workspace=Path.cwd(),
        )
        app._enqueue_task("first")
        app._enqueue_task("second")
        app._task_queue.put(None)
        worker = threading.Thread(target=app._task_worker, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(1))
        self.assertTrue(app._cancel_current_task())
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(app._pending_tasks, 0)
        self.assertIn("当前任务已取消", console.file.getvalue())
        self.assertIn("second finished", console.file.getvalue())

    def test_cancelled_permission_does_not_run_tool_or_retry_model(self):
        token = CancellationToken()
        tool = EchoTool()
        model = QueueLLM([{"action": {"tool": "echo", "args": {"text": "hi"}}}])
        events = []

        def approve(*args):
            token.cancel()
            return True

        runtime = AgentRuntime(model, ToolExecutor([tool], permission_handler=approve))
        with self.assertRaises(RunCancelled):
            runtime.run("test", cancellation=token, on_event=events.append)
        self.assertEqual(events[-1].type, EventType.RUN_CANCELLED)
        self.assertNotIn(EventType.TOOL_COMPLETED, [event.type for event in events])
        self.assertNotIn(EventType.RUN_FAILED, [event.type for event in events])
        self.assertEqual(len(model.calls), 1)

    def test_cancel_interrupts_rate_limit_wait(self):
        model = ChatCompletionsLLM(model="test", base_url="http://unused", max_rpm=1)
        model._request_times.append(time.monotonic())
        token = CancellationToken()
        errors = []
        ready = threading.Event()

        def work():
            try:
                with token.bind():
                    ready.set()
                    model._wait_for_rate_limit()
            except RunCancelled:
                errors.append("cancelled")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1))
        token.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])

    def test_cancel_closes_stalled_http_before_headers_and_during_body(self):
        for headers_sent in (False, True):
            with self.subTest(headers_sent=headers_sent):
                ready = threading.Event()
                disconnected = threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        self.rfile.read(int(self.headers["Content-Length"]))
                        if headers_sent:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.end_headers()
                            self.wfile.flush()
                        self.connection.settimeout(3)
                        ready.set()
                        try:
                            if self.connection.recv(1) == b"":
                                disconnected.set()
                        except ConnectionResetError:
                            disconnected.set()
                        except OSError:
                            pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                token = CancellationToken()
                errors = []
                model = ChatCompletionsLLM(
                    model="test", base_url=f"http://127.0.0.1:{server.server_port}",
                    timeout=3,
                )

                def work():
                    try:
                        with token.bind():
                            model.stream_chat([{"role": "user", "content": "hello"}])
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=work, daemon=True)
                try:
                    thread.start()
                    self.assertTrue(ready.wait(2))
                    token.cancel()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], RunCancelled)
                    self.assertTrue(disconnected.wait(1))
                finally:
                    token.cancel()
                    server.shutdown()
                    server.server_close()
                    server_thread.join(1)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_shell_cancellation_kills_descendant_and_allows_next_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            code = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(30)"
            )
            tool = ShellTool(directory)
            token = CancellationToken()
            errors = []

            def work():
                try:
                    with token.bind():
                        tool.run({"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                token.cancel()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0], RunCancelled)
                status = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                ).stdout.strip()
                self.assertTrue(not status or status.startswith("Z"), status)
                with CancellationToken().bind():
                    self.assertIn("next", tool.run({"command": "echo next"}))
            finally:
                token.cancel()
                thread.join(2)
