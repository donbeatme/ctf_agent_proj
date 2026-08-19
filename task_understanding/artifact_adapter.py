"""JSON-safe adapter for attachments artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


BinaryValue = bytes | bytearray | memoryview


def _binary_size(value: BinaryValue) -> int:
    if isinstance(value, memoryview):
        return value.nbytes
    return len(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted_binary": True, "size_bytes": _binary_size(value)}
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return [_json_safe(v) for v in value]
    return value


def _image_to_json_safe(image: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in image.items():
        if key == "bytes":
            safe["size_bytes"] = (
                _binary_size(value)
                if isinstance(value, (bytes, bytearray, memoryview))
                else None
            )
            continue
        safe[str(key)] = _json_safe(value)
    return safe


def artifact_to_json_safe(artifact: dict) -> dict:
    """Convert one attachments Artifact dict to a JSON-serializable dict."""

    return {
        "text": _json_safe(artifact.get("text", "")),
        "images": [
            _image_to_json_safe(image)
            for image in (artifact.get("images") or [])
            if isinstance(image, Mapping)
        ],
        "audio": _json_safe(artifact.get("audio", [])),
        "video": _json_safe(artifact.get("video", [])),
        "meta": _json_safe(artifact.get("meta", {})),
    }


def artifacts_to_json_safe(artifacts) -> list[dict]:
    """Convert an iterable of attachments artifacts to JSON-safe dicts."""

    return [artifact_to_json_safe(dict(artifact)) for artifact in artifacts]


def contains_binary(value: Any) -> bool:
    """Return True when a structure still contains bytes-like values."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, Mapping):
        return any(contains_binary(v) for v in value.values())
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return any(contains_binary(v) for v in value)
    return False
