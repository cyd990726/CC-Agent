"""Provider-neutral LLM interface and an OpenAI-compatible HTTP adapter."""

import json
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class ModelError(RuntimeError):
    """Raised when a model request or response is invalid."""


class ModelProtocolError(ModelError):
    """Raised when model content does not follow the agent JSON protocol."""


# 抽象基类
class LLM(ABC):
    """Minimal interface required by the agent runtime."""

    # 和LLM的子类交互的主要方法是chat方法，它接受一个消息序列，并返回一个动作协议的JSON对象。这个方法是抽象的，必须在子类中实现。
    @abstractmethod
    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """Return one action-protocol JSON object."""

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        on_delta: Callable[[str], None] | None = None,
    ) -> Mapping[str, Any]:
        """Return one response, optionally reporting content increments.

        Adapters without native streaming support keep working through this
        fallback. They intentionally do not synthesize a delta because callers
        will render the completed response normally.
        """

        return self.chat(messages)


class ChatCompletionsLLM(LLM):
    """Call any provider exposing an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        max_rpm: int | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")
        if not base_url.strip():
            raise ValueError("base_url cannot be empty")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if max_rpm is not None and max_rpm < 1:
            raise ValueError("max_rpm must be at least 1")
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_rpm = max_rpm
        self._request_times: deque[float] = deque()

    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        payload = self._make_payload(messages, stream=False)
        body = self._send(payload).decode("utf-8")

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("model API returned an unexpected response") from exc
        if not isinstance(content, str):
            raise ModelError("model message content must be a string")
        return parse_json_object(content)

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        on_delta: Callable[[str], None] | None = None,
    ) -> Mapping[str, Any]:
        """Consume an OpenAI-compatible SSE stream and return its full JSON."""

        payload = self._make_payload(messages, stream=True)
        chunks: list[str] = []

        def consume(response: Any) -> None:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_text = line.removeprefix("data:").strip()
                if data_text == "[DONE]":
                    break
                try:
                    event = json.loads(data_text)
                    delta = event["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    raise ModelError("model API returned an invalid stream event") from exc
                if delta is None:
                    continue
                if not isinstance(delta, str):
                    raise ModelError("stream content delta must be a string")
                chunks.append(delta)
                if on_delta is not None:
                    on_delta(delta)

        self._open_with_retries(payload, consume)
        if not chunks:
            raise ModelError("model API returned an empty stream")
        return parse_json_object("".join(chunks))

    def _make_payload(
        self, messages: Sequence[Mapping[str, str]], *, stream: bool
    ) -> bytes:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if stream:
            body["stream"] = True
        return json.dumps(body).encode("utf-8")

    def _send(self, payload: bytes) -> bytes:
        body: bytes | None = None

        def consume(response: Any) -> None:
            nonlocal body
            body = response.read()

        self._open_with_retries(payload, consume)
        if body is None:
            raise ModelError("model API returned no response")
        return body

    def _open_with_retries(
        self, payload: bytes, consume: Callable[[Any], None]
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint, data=payload, headers=headers, method="POST"
        )

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            self._request_times.append(time.monotonic())
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    consume(response)
                return
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                finally:
                    exc.close()
                if exc.code == 429 and attempt < self.max_retries:
                    detected_rpm = self._detect_max_rpm(detail)
                    if detected_rpm is not None:
                        self.max_rpm = (
                            detected_rpm
                            if self.max_rpm is None
                            else min(self.max_rpm, detected_rpm)
                        )
                        # The rejected request did not consume provider capacity.
                        self._request_times.pop()
                    time.sleep(self._retry_delay(exc, detail, attempt))
                    continue
                raise ModelError(
                    f"model request failed with HTTP {exc.code}: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                raise ModelError(f"model request failed: {exc.reason}") from exc

    def _wait_for_rate_limit(self) -> None:
        """Keep request starts inside the provider's rolling one-minute quota."""

        if self.max_rpm is None:
            return
        while True:
            now = time.monotonic()
            cutoff = now - 60.0
            while self._request_times and self._request_times[0] <= cutoff:
                self._request_times.popleft()
            if len(self._request_times) < self.max_rpm:
                return
            time.sleep(max(0.0, self._request_times[0] + 60.0 - now))

    @staticmethod
    def _detect_max_rpm(detail: str) -> int | None:
        match = re.search(r"max\s+RPM\s*:\s*(\d+)", detail, re.I)
        if match is None:
            return None
        value = int(match.group(1))
        return value if value > 0 else None

    @staticmethod
    def _retry_delay(
        error: urllib.error.HTTPError, detail: str, attempt: int
    ) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 60.0))
            except ValueError:
                pass
        match = re.search(r"try again after ([0-9.]+) seconds?", detail, re.I)
        if match:
            return max(0.0, min(float(match.group(1)), 60.0))
        return min(2**attempt, 60.0)


def parse_json_object(content: str) -> Mapping[str, Any]:
    """Parse a JSON object, tolerating a single Markdown JSON code fence."""

    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelProtocolError("model did not return valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelProtocolError("model response JSON must be an object")
    return value
