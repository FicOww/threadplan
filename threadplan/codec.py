"""Shared JSON/schema/time boundary utilities; no conversation semantics."""


import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any


class ModelCallError(RuntimeError):
    """The single injected model call failed; no retry was attempted."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non_json_constant:{value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _schema_errors(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """Validate the small JSON Schema vocabulary used by model carriers."""
    if "anyOf" in schema:
        return [] if any(not _schema_errors(value, option, path)
                         for option in schema["anyOf"]) else [f"{path}:invalid_type_or_value"]
    type_name = schema.get("type")
    matches = {"object": type(value) is dict, "array": type(value) is list,
               "string": type(value) is str, "null": value is None, "boolean": type(value) is bool,
               "integer": type(value) is int,
               "number": type(value) is int or (type(value) is float and math.isfinite(value))}
    if type_name is not None and not matches[type_name]:
        return [f"{path}:expected_{type_name}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}:invalid_enum"]
    errors = []
    if type_name == "object":
        properties = schema["properties"]
        if any(type(key) is not str for key in value):
            return [f"{path}:non_string_key"]
        if schema.get("additionalProperties") is not True:
            for key in value.keys() - properties.keys():
                errors.append(f"{path}.{key}:unknown_field")
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}:missing_field")
        for key in value.keys() & properties.keys():
            errors.extend(_schema_errors(value[key], properties[key], f"{path}.{key}"))
    elif type_name == "array":
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}:too_few_items")
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            errors.append(f"{path}:duplicate_items")
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    elif type_name == "string":
        if len(value) < schema.get("minLength", 0) or ("pattern" in schema and not re.search(schema["pattern"], value)):
            errors.append(f"{path}:empty_text")
    return errors


def canonical_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("not_json_serializable") from exc


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _aware_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    # fromisoformat normalizes overflowing offset components (+00:60 becomes
    # +01:00). Reject that malformed source instead of changing its meaning.
    offset = re.search(r"[+-](\d{2})(?::?(\d{2}))?(?::?(\d{2}))?(?:[.,]\d+)?$", value)
    if offset is not None and (int(offset[1]) >= 24
                               or (offset[2] is not None and int(offset[2]) >= 60)
                               or (offset[3] is not None and int(offset[3]) >= 60)):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None
