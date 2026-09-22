import json
import urllib.error
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from model.llm import ChatCompletionsLLM, ModelError, parse_json_object


class ParseModelResponseTests(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        self.assertEqual(
            parse_json_object('{"final_answer":"ok"}'), {"final_answer": "ok"}
        )

    def test_parses_json_code_fence(self) -> None:
        self.assertEqual(
            parse_json_object('```json\n{"action":{"tool":"x","args":{}}}\n```'),
            {"action": {"tool": "x", "args": {}}},
        )

    def test_rejects_non_object_json(self) -> None:
        with self.assertRaises(ModelError):
            parse_json_object("[]")


class ChatCompletionsLLMTests(unittest.TestCase):
    def test_calls_compatible_endpoint_and_parses_message(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {"message": {"content": '{"final_answer":"finished"}'}}
                ]
            }
        ).encode()
        model = ChatCompletionsLLM(
            model="test-model",
            base_url="https://example.test/v1/",
            api_key="secret",
        )

        with patch("model.llm.urllib.request.urlopen", return_value=response) as call:
            result = model.chat([{"role": "user", "content": "hello"}])

        request = call.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(payload["model"], "test-model")
        self.assertNotIn("temperature", payload)
        self.assertEqual(result, {"final_answer": "finished"})

    def test_streams_content_deltas_and_parses_complete_message(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"final_"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"answer\\":\\"ok\\"}"}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )
        deltas: list[str] = []

        with patch("model.llm.urllib.request.urlopen", return_value=response) as call:
            result = model.stream_chat(
                [{"role": "user", "content": "hello"}], deltas.append
            )

        payload = json.loads(call.call_args.args[0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(deltas, ['{"final_', 'answer":"ok"}'])
        self.assertEqual(result, {"final_answer": "ok"})

    def test_retries_rate_limit_using_server_delay(self) -> None:
        rate_limit = urllib.error.HTTPError(
            "https://example.test/v1/chat/completions",
            429,
            "rate limited",
            {"Retry-After": "1"},
            BytesIO(b'{"error":{"message":"rate limited"}}'),
        )
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = (
            b'{"choices":[{"message":{"content":"{\\"final_answer\\":'
            b'\\"finished\\"}"}}]}'
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with (
            patch(
                "model.llm.urllib.request.urlopen",
                side_effect=[rate_limit, response],
            ) as request,
            patch("model.llm.time.sleep") as sleep,
        ):
            result = model.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(result, {"final_answer": "finished"})

    def test_detects_provider_rpm_limit(self) -> None:
        detail = (
            '{"error":{"message":"request reached organization max RPM: 3, '
            'please try again after 1 seconds"}}'
        )

        self.assertEqual(ChatCompletionsLLM._detect_max_rpm(detail), 3)

    def test_waits_until_rolling_rpm_window_has_capacity(self) -> None:
        model = ChatCompletionsLLM(
            model="test-model",
            base_url="https://example.test/v1",
            max_rpm=2,
        )
        model._request_times.extend([0.0, 1.0])
        clock = [2.0]

        def advance(seconds: float) -> None:
            clock[0] += seconds

        with (
            patch("model.llm.time.monotonic", side_effect=lambda: clock[0]),
            patch("model.llm.time.sleep", side_effect=advance) as sleep,
        ):
            model._wait_for_rate_limit()

        sleep.assert_called_once_with(58.0)


if __name__ == "__main__":
    unittest.main()
