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


if __name__ == "__main__":
    unittest.main()
