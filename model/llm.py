"""Provider-neutral LLM interface and an OpenAI-compatible HTTP adapter."""

import json
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


class ModelError(RuntimeError):
    """Raised when a model request or response is invalid."""

# 抽象基类
class LLM(ABC):
    """Minimal interface required by the agent runtime."""

    # 和LLM的子类交互的主要方法是chat方法，它接受一个消息序列，并返回一个动作协议的JSON对象。这个方法是抽象的，必须在子类中实现。
    @abstractmethod
    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """Return one action-protocol JSON object."""


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
    ) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")
        if not base_url.strip():
            raise ValueError("base_url cannot be empty")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": list(messages),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint, data=payload, headers=headers, method="POST"
        )

        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < self.max_retries:
                    time.sleep(self._retry_delay(exc, detail, attempt))
                    continue
                raise ModelError(
                    f"model request failed with HTTP {exc.code}: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                raise ModelError(f"model request failed: {exc.reason}") from exc

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("model API returned an unexpected response") from exc
        if not isinstance(content, str):
            raise ModelError("model message content must be a string")
        return parse_json_object(content)

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
        raise ModelError("model did not return valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelError("model response JSON must be an object")
    return value
