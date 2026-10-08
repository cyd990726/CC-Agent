import math
import unittest
from collections.abc import Mapping
from typing import Any

from tools.base import Tool, ToolExecutor
from tools.schema import (
    SchemaDefinitionError,
    SchemaValidationError,
    validate_schema,
    validate_value,
)


class RecordingTool(Tool):
    name = "record"
    description = "Record structured input."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def run(self, args: Mapping[str, Any]) -> str:
        self.calls.append(args)
        return "recorded"


class SchemaDefinitionTests(unittest.TestCase):
    def test_accepts_the_supported_schema_subset(self) -> None:
        schema = {
            "type": "object",
            "description": "A complete supported schema",
            "properties": {
                "text": {
                    "type": "string",
                    "enum": ["a", "b"],
                    "minLength": 1,
                },
                "integer": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "exclusiveMinimum": -1,
                    "exclusiveMaximum": 11,
                },
                "number": {"type": "number", "minimum": 0.5},
                "enabled": {"type": "boolean"},
                "optional": {"type": "boolean", "default": False},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 2,
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        }

        validate_schema(schema)

    def test_rejects_unknown_types_and_keywords(self) -> None:
        with self.assertRaisesRegex(SchemaDefinitionError, "schema.type"):
            validate_schema({"type": "null"})
        with self.assertRaisesRegex(SchemaDefinitionError, "schema.pattern"):
            validate_schema({"type": "string", "pattern": "unsupported"})

    def test_default_is_an_annotation_and_must_be_json_data(self) -> None:
        validate_schema({"type": "boolean", "default": False})
        with self.assertRaisesRegex(SchemaDefinitionError, "finite JSON value"):
            validate_schema({"type": "number", "default": math.inf})

    def test_rejects_invalid_nested_definitions(self) -> None:
        with self.assertRaisesRegex(
            SchemaDefinitionError, r"schema\.properties\.path\.minLength"
        ):
            validate_schema(
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": True}
                    },
                }
            )
        with self.assertRaisesRegex(SchemaDefinitionError, "finite number"):
            validate_schema({"type": "number", "minimum": math.inf})
        with self.assertRaisesRegex(SchemaDefinitionError, "finite JSON value"):
            validate_schema({"type": "number", "enum": [math.nan]})
        with self.assertRaisesRegex(SchemaDefinitionError, "minItems"):
            validate_schema({"type": "array", "minItems": 2, "maxItems": 1})

    def test_executor_validates_schemas_at_registration(self) -> None:
        class InvalidTool(Tool):
            name = "invalid"
            description = "Invalid."
            input_schema = {"type": "object", "required": "path"}

            def run(self, args: Mapping[str, Any]) -> str:
                return "unused"

        with self.assertRaisesRegex(
            ValueError, r"invalid schema for tool 'invalid'.*required"
        ):
            ToolExecutor([InvalidTool()])

    def test_executor_validates_output_schema_at_registration(self) -> None:
        class InvalidOutputTool(RecordingTool):
            output_schema = {"type": "string", "minimum": 1}

        with self.assertRaisesRegex(ValueError, "output_schema.minimum"):
            ToolExecutor([InvalidOutputTool()])

    def test_executor_validates_tool_metadata_at_registration(self) -> None:
        class InvalidMetadataTool(RecordingTool):
            max_output_chars = 0

        with self.assertRaisesRegex(ValueError, "max_output_chars"):
            ToolExecutor([InvalidMetadataTool()])


class SchemaValueTests(unittest.TestCase):
    def test_validates_nested_objects_with_stable_paths(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            },
            "required": ["path", "nested"],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(SchemaValidationError, r"args\.path"):
            validate_value({"nested": {"enabled": True}}, schema, path="args")
        with self.assertRaisesRegex(SchemaValidationError, r"args\.nested\.extra"):
            validate_value(
                {"path": "x", "nested": {"enabled": True, "extra": 1}},
                schema,
                path="args",
            )

    def test_rejects_bool_and_non_finite_numeric_values(self) -> None:
        for schema_type in ("integer", "number"):
            with self.subTest(schema_type=schema_type, value=True):
                with self.assertRaises(SchemaValidationError):
                    validate_value(True, {"type": schema_type})
            for value in (math.inf, -math.inf, math.nan):
                with self.subTest(schema_type=schema_type, value=value):
                    with self.assertRaises(SchemaValidationError):
                        validate_value(value, {"type": schema_type})

    def test_validates_inclusive_and_exclusive_number_bounds(self) -> None:
        validate_value(
            2,
            {
                "type": "integer",
                "minimum": 2,
                "maximum": 2,
                "exclusiveMinimum": 1,
                "exclusiveMaximum": 3,
            },
        )
        cases = (
            ({"minimum": 2}, 1),
            ({"maximum": 2}, 3),
            ({"exclusiveMinimum": 2}, 2),
            ({"exclusiveMaximum": 2}, 2),
        )
        for constraint, value in cases:
            with self.subTest(constraint=constraint):
                with self.assertRaises(SchemaValidationError):
                    validate_value(value, {"type": "number", **constraint})

    def test_validates_array_items_and_lengths(self) -> None:
        schema = {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 2,
        }
        validate_value([1, 2], schema, path="args.values")
        with self.assertRaisesRegex(SchemaValidationError, r"args\.values\[1\]"):
            validate_value([1, "two"], schema, path="args.values")
        with self.assertRaisesRegex(SchemaValidationError, "at least 1"):
            validate_value([], schema)
        with self.assertRaisesRegex(SchemaValidationError, "at most 2"):
            validate_value([1, 2, 3], schema)

    def test_enum_comparison_does_not_treat_bool_as_integer(self) -> None:
        validate_value(1, {"type": "integer", "enum": [1, 2]})
        with self.assertRaises(SchemaValidationError):
            validate_value(True, {"type": "integer", "enum": [1, 2]})


class ToolExecutorSchemaTests(unittest.TestCase):
    def test_describe_preserves_shape_and_exposes_input_schema_as_args(self) -> None:
        tool = RecordingTool()

        self.assertEqual(
            tool.describe(),
            {
                "name": "record",
                "description": "Record structured input.",
                "args": RecordingTool.input_schema,
            },
        )
        self.assertFalse(tool.read_only)
        self.assertFalse(tool.concurrency_safe)
        self.assertFalse(tool.destructive)
        self.assertEqual(tool.output_schema, {"type": "string"})
        self.assertGreater(tool.max_output_chars, 0)

    def test_invalid_args_are_recoverable_and_checked_before_permission(self) -> None:
        tool = RecordingTool()
        permission_calls: list[Mapping[str, Any]] = []

        def approve(_name: str, args: Mapping[str, Any]) -> bool:
            permission_calls.append(args)
            return True

        result = ToolExecutor([tool], permission_handler=approve).execute(
            "record", {"path": "", "unknown": True}
        )

        self.assertFalse(result.success)
        self.assertEqual(result.output, "args.path: length must be at least 1")
        self.assertEqual(permission_calls, [])
        self.assertEqual(tool.calls, [])

    def test_missing_optional_values_are_not_defaulted(self) -> None:
        tool = RecordingTool()
        args = {"path": "file.txt"}

        result = ToolExecutor([tool]).execute("record", args)

        self.assertTrue(result.success)
        self.assertNotIn("count", args)
        self.assertNotIn("count", tool.calls[0])

    def test_legacy_args_schema_remains_compatible(self) -> None:
        class LegacyTool(Tool):
            name = "legacy"
            description = "Legacy custom tool."
            args_schema = {"text": "string"}

            def run(self, args: Mapping[str, Any]) -> str:
                return str(args["text"])

        tool = LegacyTool()
        executor = ToolExecutor([tool])

        self.assertEqual(tool.describe()["args"], {"text": "string"})
        self.assertTrue(executor.execute("legacy", {"text": "ok"}).success)

    def test_string_output_is_validated_after_run(self) -> None:
        class ShortOutputTool(RecordingTool):
            output_schema = {"type": "string", "minLength": 10}

        result = ToolExecutor([ShortOutputTool()]).execute(
            "record", {"path": "file.txt"}
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.output,
            "SchemaValidationError: output: length must be at least 10",
        )


if __name__ == "__main__":
    unittest.main()
