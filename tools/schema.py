"""Small, dependency-free JSON Schema validation for tool contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_TYPES = frozenset(
    {"object", "string", "integer", "number", "boolean", "array"}
)


class SchemaDefinitionError(ValueError):
    """Raised when a tool declares an invalid or unsupported schema."""


class SchemaValidationError(ValueError):
    """Raised when a value does not satisfy a schema."""


_COMMON_KEYWORDS = frozenset({"type", "enum", "description", "default"})
_TYPE_KEYWORDS = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "string": frozenset({"minLength"}),
    "integer": frozenset(
        {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
    ),
    "number": frozenset(
        {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
    ),
    "boolean": frozenset(),
    "array": frozenset({"items", "minItems", "maxItems"}),
}


def validate_schema(schema: Mapping[str, Any], *, path: str = "schema") -> None:
    """Validate that *schema* uses the supported JSON Schema subset."""

    if not isinstance(schema, Mapping):
        raise SchemaDefinitionError(f"{path}: schema must be an object")

    schema_type = schema.get("type")
    if schema_type not in SCHEMA_TYPES:
        supported = ", ".join(sorted(SCHEMA_TYPES))
        raise SchemaDefinitionError(
            f"{path}.type: expected one of {supported}"
        )

    unknown = set(schema) - _COMMON_KEYWORDS - _TYPE_KEYWORDS[schema_type]
    if unknown:
        keyword = sorted(unknown)[0]
        raise SchemaDefinitionError(
            f"{path}.{keyword}: unsupported keyword for type {schema_type}"
        )

    description = schema.get("description")
    if description is not None and not isinstance(description, str):
        raise SchemaDefinitionError(f"{path}.description: expected string")

    if "default" in schema and not _is_json_value(schema["default"]):
        raise SchemaDefinitionError(
            f"{path}.default: expected finite JSON value"
        )

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, Sequence) or isinstance(enum, (str, bytes)):
            raise SchemaDefinitionError(f"{path}.enum: expected non-empty array")
        if not enum:
            raise SchemaDefinitionError(f"{path}.enum: expected non-empty array")
        for index, value in enumerate(enum):
            if not _is_json_value(value):
                raise SchemaDefinitionError(
                    f"{path}.enum[{index}]: expected finite JSON value"
                )
            if not _matches_type(value, schema_type):
                raise SchemaDefinitionError(
                    f"{path}.enum[{index}]: expected {schema_type}"
                )

    if schema_type == "object":
        _validate_object_schema(schema, path)
    elif schema_type == "string":
        _validate_non_negative_integer(schema, "minLength", path)
    elif schema_type in {"integer", "number"}:
        for keyword in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ):
            _validate_finite_number(schema, keyword, path)
    elif schema_type == "array":
        if "items" in schema:
            validate_schema(schema["items"], path=f"{path}.items")
        _validate_non_negative_integer(schema, "minItems", path)
        _validate_non_negative_integer(schema, "maxItems", path)
        if schema.get("minItems", 0) > schema.get("maxItems", math.inf):
            raise SchemaDefinitionError(
                f"{path}: minItems cannot be greater than maxItems"
            )


def validate_value(
    value: Any, schema: Mapping[str, Any], *, path: str = "value"
) -> None:
    """Validate *value* and raise a path-aware error on the first mismatch."""

    schema_type = schema["type"]
    if not _matches_type(value, schema_type):
        raise SchemaValidationError(f"{path}: expected {schema_type}")

    if schema_type in {"integer", "number"} and not math.isfinite(value):
        raise SchemaValidationError(f"{path}: expected a finite {schema_type}")

    if "enum" in schema and not any(
        _json_equal(value, candidate) for candidate in schema["enum"]
    ):
        raise SchemaValidationError(f"{path}: value is not in enum")

    if schema_type == "object":
        _validate_object_value(value, schema, path)
    elif schema_type == "string":
        minimum = schema.get("minLength")
        if minimum is not None and len(value) < minimum:
            raise SchemaValidationError(
                f"{path}: length must be at least {minimum}"
            )
    elif schema_type in {"integer", "number"}:
        _validate_number_value(value, schema, path)
    elif schema_type == "array":
        _validate_array_value(value, schema, path)


def validate_instance(
    value: Any, schema: Mapping[str, Any], *, path: str = "value"
) -> None:
    """Compatibility alias for callers that use JSON Schema terminology."""

    validate_value(value, schema, path=path)


def _validate_object_schema(schema: Mapping[str, Any], path: str) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaDefinitionError(f"{path}.properties: expected object")
    for name, child_schema in properties.items():
        if not isinstance(name, str):
            raise SchemaDefinitionError(
                f"{path}.properties: property names must be strings"
            )
        validate_schema(child_schema, path=f"{path}.properties.{name}")

    required = schema.get("required", [])
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
        raise SchemaDefinitionError(f"{path}.required: expected array of strings")
    seen: set[str] = set()
    for index, name in enumerate(required):
        if not isinstance(name, str):
            raise SchemaDefinitionError(
                f"{path}.required[{index}]: expected string"
            )
        if name in seen:
            raise SchemaDefinitionError(
                f"{path}.required[{index}]: duplicate property {name!r}"
            )
        seen.add(name)

    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise SchemaDefinitionError(
            f"{path}.additionalProperties: expected boolean"
        )


def _validate_non_negative_integer(
    schema: Mapping[str, Any], keyword: str, path: str
) -> None:
    if keyword not in schema:
        return
    value = schema[keyword]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SchemaDefinitionError(
            f"{path}.{keyword}: expected non-negative integer"
        )


def _validate_finite_number(
    schema: Mapping[str, Any], keyword: str, path: str
) -> None:
    if keyword not in schema:
        return
    value = schema[keyword]
    if not _matches_type(value, "number") or not math.isfinite(value):
        raise SchemaDefinitionError(f"{path}.{keyword}: expected finite number")


def _matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    return False


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(name, str) and _is_json_value(item)
            for name, item in value.items()
        )
    return False


def _validate_object_value(
    value: Mapping[str, Any], schema: Mapping[str, Any], path: str
) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            raise SchemaValidationError(
                f"{_property_path(path, name)}: required property is missing"
            )
    for name, child_value in value.items():
        child_path = _property_path(path, name)
        if not isinstance(name, str):
            raise SchemaValidationError(f"{child_path}: property name must be a string")
        if name in properties:
            validate_value(child_value, properties[name], path=child_path)
        elif schema.get("additionalProperties", True) is False:
            raise SchemaValidationError(
                f"{child_path}: additional property is not allowed"
            )


def _validate_number_value(
    value: int | float, schema: Mapping[str, Any], path: str
) -> None:
    checks = (
        ("minimum", lambda bound: value >= bound, "greater than or equal to"),
        ("maximum", lambda bound: value <= bound, "less than or equal to"),
        ("exclusiveMinimum", lambda bound: value > bound, "greater than"),
        ("exclusiveMaximum", lambda bound: value < bound, "less than"),
    )
    for keyword, predicate, wording in checks:
        if keyword in schema and not predicate(schema[keyword]):
            raise SchemaValidationError(
                f"{path}: must be {wording} {schema[keyword]}"
            )


def _validate_array_value(
    value: list[Any], schema: Mapping[str, Any], path: str
) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if minimum is not None and len(value) < minimum:
        raise SchemaValidationError(f"{path}: must contain at least {minimum} items")
    if maximum is not None and len(value) > maximum:
        raise SchemaValidationError(f"{path}: must contain at most {maximum} items")
    item_schema = schema.get("items")
    if item_schema is not None:
        for index, item in enumerate(value):
            validate_value(item, item_schema, path=f"{path}[{index}]")


def _property_path(path: str, name: Any) -> str:
    if isinstance(name, str) and name.isidentifier():
        return f"{path}.{name}"
    return f"{path}[{name!r}]"
