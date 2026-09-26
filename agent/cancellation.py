"""Per-run cooperative cancellation, including interruptible I/O waits."""

import contextvars
import queue
import threading
from contextlib import contextmanager


class RunCancelled(Exception):
    """The user cancelled this run; never turn this into a tool observation."""


current_token = contextvars.ContextVar("cancellation_token", default=None)


class CancellationToken:
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.RLock()
        self._callbacks = set()

    def check(self):
        if self.event.is_set():
            raise RunCancelled("当前任务已取消")

    def cancel(self):
        self.event.set()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback()

    def wait(self, seconds):
        self.event.wait(seconds)
        self.check()

    @contextmanager
    def bind(self):
        binding = current_token.set(self)
        try:
            self.check()
            yield
        finally:
            current_token.reset(binding)

    @contextmanager
    def on_cancel(self, callback):
        with self._lock:
            self._callbacks.add(callback)
            cancelled = self.event.is_set()
        try:
            if cancelled:
                callback()
            self.check()
            yield
        finally:
            with self._lock:
                self._callbacks.discard(callback)

    def deliver(self, callback, *args):
        # A cancelled request must not emit late deltas into the next task.
        with self._lock:
            self.check()
            return callback(*args)

    def run_io(self, operation):
        """Release the caller even when DNS/connect blocks in the OS.

        Only use for read-only model I/O, never for tools with side effects.
        The adapter closes connected sockets on cancel and checks the token
        before consuming any response that arrives after cancellation.
        """
        self.check()
        result = queue.Queue(maxsize=1)
        context = contextvars.copy_context()

        def work():
            try:
                result.put((True, context.run(operation)))
            except BaseException as exc:
                result.put((False, exc))

        threading.Thread(target=work, daemon=True, name="model-request").start()
        while True:
            self.check()
            try:
                success, value = result.get(timeout=0.05)
            except queue.Empty:
                continue
            self.check()
            if success:
                return value
            raise value


def check_cancelled():
    token = current_token.get()
    if token is not None:
        token.check()
