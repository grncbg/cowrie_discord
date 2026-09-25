"""Validate untrusted JSON before using concrete types."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import TypeGuard


def is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)


def json_object(value: object) -> dict[str, object]:
    if not is_mapping(value):
        raise ValueError("Expected a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("Expected string keys")
        result[key] = item
    return result


def json_array(value: object) -> list[object]:
    if not is_sequence(value):
        raise ValueError("Expected a JSON array")
    return list(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def nonempty_string(value: object) -> str:
    result = string(value)
    if not result or "\x00" in result:
        raise ValueError("Expected a nonempty string without NUL")
    return result


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean")
    return value


def nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("interval_seconds must be a nonnegative integer")
    return value


def parse_json(data: str | bytes) -> object:
    value: object = json.loads(data)
    return value


def read_json(path: Path) -> object:
    with path.open(encoding="utf-8-sig") as stream:
        return parse_json(stream.read())
