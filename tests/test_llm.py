import json
import urllib.error
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from model.llm import (
    ChatCompletionsLLM,
    ModelError,
    ModelProtocolError,
    TokenUsage,
    parse_json_object,
)


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

    def test_rejects_multiple_action_objects(self) -> None:
        content = (
            '{"thought":"first","action":{"tool":"x","args":{}}}\n\n'
            '{"thought":"second","action":{"tool":"y","args":{}}}'
        )

        with self.assertRaisesRegex(ModelError, "multiple JSON action objects"):
            parse_json_object(content)

    def test_parses_object_with_surrounding_prose(self) -> None:
        content = (
            "Here is the next step:\n\n"
            '{"final_answer":"ok"}\n\n'
            "Let me wait for the observation."
        )

        self.assertEqual(parse_json_object(content), {"final_answer": "ok"})

    def test_parses_object_inside_fence_with_prose(self) -> None:
        content = (
            "Sure, I will inspect the files.\n"
            '```json\n{"action":{"tool":"list_files","args":{}}}\n```\n'
            "Waiting for the result."
        )

        self.assertEqual(
            parse_json_object(content),
            {"action": {"tool": "list_files", "args": {}}},
        )

    def test_rejects_content_without_json_object(self) -> None:
        with self.assertRaisesRegex(ModelError, "did not return valid JSON"):
            parse_json_object("I cannot use tools right now.")

    def test_skips_embedded_objects_that_are_not_actions(self) -> None:
        content = (
            'For example {"thought":"demo"} is not a step. '
            'The real step is {"final_answer":"ok"}.'
        )

        self.assertEqual(parse_json_object(content), {"final_answer": "ok"})

    def test_rejects_prose_example_next_to_a_real_action(self) -> None:
        content = (
            'You could call {"action":{"tool":"shell","args":{"command":"rm -rf build"}}}.\n'
            'My actual next step:\n'
            '{"action":{"tool":"read_file","args":{"path":"main.py"}}}'
        )

        with self.assertRaisesRegex(ModelError, "multiple JSON action objects"):
            parse_json_object(content)

    def test_rejects_truncated_action_inside_larger_object(self) -> None:
        content = '{"thought":"reading","action":{"tool":"read_file","args":{"path":"main.py"}'

        with self.assertRaisesRegex(ModelError, "did not return valid JSON"):
            parse_json_object(content)

    def test_protocol_error_carries_raw_response_for_retry(self) -> None:
        content = '{"thought":"reading","action":{"tool":"read_file","args":'

        with self.assertRaises(ModelProtocolError) as caught:
            parse_json_object(content)

        self.assertEqual(caught.exception.raw, content)


class ChatCompletionsLLMTests(unittest.TestCase):
    def test_calls_compatible_endpoint_and_parses_message(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {"message": {"content": '{"final_answer":"finished"}'}}
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
            }
        ).encode()
        model = ChatCompletionsLLM(
            model="test-model",
            base_url="https://example.test/v1/",
            api_key="secret",
        )
        usages: list[TokenUsage] = []

        with patch("model.llm.urllib.request.urlopen", return_value=response) as call:
            result = model.chat(
                [{"role": "user", "content": "hello"}],
                on_usage=usages.append,
            )

        request = call.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(payload["model"], "test-model")
        self.assertNotIn("temperature", payload)
        self.assertEqual(result, {"final_answer": "finished"})
        self.assertEqual(
            usages,
            [TokenUsage(prompt_tokens=7, completion_tokens=2, total_tokens=9)],
        )

    def test_streams_content_deltas_and_parses_complete_message(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"final_"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"answer\\":\\"ok\\"}"}}]}\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":4,"total_tokens":9}}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )
        deltas: list[str] = []
        usages: list[TokenUsage] = []

        with patch("model.llm.urllib.request.urlopen", return_value=response) as call:
            result = model.stream_chat(
                [{"role": "user", "content": "hello"}],
                deltas.append,
                on_usage=usages.append,
            )

        payload = json.loads(call.call_args.args[0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(deltas, ['{"final_', 'answer":"ok"}'])
        self.assertEqual(result, {"final_answer": "ok"})
        self.assertEqual(
            usages,
            [TokenUsage(prompt_tokens=5, completion_tokens=4, total_tokens=9)],
        )

    def test_stream_chat_with_tools_sends_openai_tool_schema(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"id":"call_1","type":"function","function":'
                b'{"name":"echo","arguments":"{\\"text\\":\\"hi\\"}"}}]},'
                b'"finish_reason":"tool_calls"}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )
        tools = [
            {
                "name": "echo",
                "description": "Echo text.",
                "args": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]

        with patch("model.llm.urllib.request.urlopen", return_value=response) as call:
            model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}], tools
            )

        payload = json.loads(call.call_args.args[0].data)
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo text.",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }
            ],
        )

    def test_stream_chat_with_tools_accumulates_fragmented_arguments(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"name":"sh"}}]}}]}\n',
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"name":"ell","arguments":"{\\"comm"}}]}}]}\n',
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"arguments":"and\\":\\"ls\\"}"}}]}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with patch("model.llm.urllib.request.urlopen", return_value=response):
            result = model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
            )

        self.assertEqual(
            result, {"action": {"tool": "shell", "args": {"command": "ls"}}}
        )

    def test_stream_chat_with_tools_returns_every_tool_call_in_order(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":['
                b'{"index":0,"function":{"name":"a","arguments":"{\\"x\\":1}"}},'
                b'{"index":1,"function":{"name":"b","arguments":"{\\"y\\":2}"}}]}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with patch("model.llm.urllib.request.urlopen", return_value=response):
            result = model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "a", "description": "A.", "args": {}}],
            )

        self.assertEqual(
            result,
            {
                "actions": [
                    {"tool": "a", "args": {"x": 1}},
                    {"tool": "b", "args": {"y": 2}},
                ]
            },
        )

    def test_stream_chat_with_tools_rejects_invalid_argument_json(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"name":"shell","arguments":"{\\"command\\":"}}]}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with (
            patch("model.llm.urllib.request.urlopen", return_value=response),
            self.assertRaisesRegex(ModelProtocolError, "not valid JSON"),
        ):
            model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
            )

    def test_stream_chat_with_tools_rejects_non_object_arguments(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"name":"shell","arguments":"[]"}}]}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with (
            patch("model.llm.urllib.request.urlopen", return_value=response),
            self.assertRaisesRegex(ModelProtocolError, "must be an object"),
        ):
            model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
            )

    def test_stream_chat_with_tools_falls_back_to_text_protocol(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"final_ans"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"wer\\":\\"done\\"}"}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with patch("model.llm.urllib.request.urlopen", return_value=response):
            result = model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
            )

        self.assertEqual(result, {"final_answer": "done"})

    def test_stream_chat_with_tools_rejects_empty_response(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter([b"data: [DONE]\n"])
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )

        with (
            patch("model.llm.urllib.request.urlopen", return_value=response),
            self.assertRaisesRegex(ModelProtocolError, "neither a tool call"),
        ):
            model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
            )

    def test_stream_chat_with_tools_reports_usage(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"name":"shell","arguments":"{}"}}]}}]}\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":4,"total_tokens":9}}\n',
                b"data: [DONE]\n",
            ]
        )
        model = ChatCompletionsLLM(
            model="test-model", base_url="https://example.test/v1"
        )
        usages: list[TokenUsage] = []

        with patch("model.llm.urllib.request.urlopen", return_value=response):
            model.stream_chat_with_tools(
                [{"role": "user", "content": "hello"}],
                [{"name": "shell", "description": "Run.", "args": {}}],
                on_usage=usages.append,
            )

        self.assertEqual(
            usages,
            [TokenUsage(prompt_tokens=5, completion_tokens=4, total_tokens=9)],
        )

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
