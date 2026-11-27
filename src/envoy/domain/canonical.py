from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import cbor2


def normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Canonical datetime must be timezone aware")
        return cbor2.CBORTag(0, value.astimezone(UTC).isoformat(timespec="microseconds"))
    if isinstance(value, timedelta):
        return cbor2.CBORTag(1002, [value.days, value.seconds, value.microseconds])
    if isinstance(value, UUID):
        return cbor2.CBORTag(37, value.bytes)
    return value


def encode(value: Any) -> bytes:
    return bytes(cbor2.dumps(normalize(value), canonical=True))


def _tag_hook(tag: cbor2.CBORTag, immutable: bool) -> Any:
    if tag.tag == 1002:
        days, seconds, microseconds = tag.value
        return timedelta(days=days, seconds=seconds, microseconds=microseconds)
    return tag


def decode(data: bytes) -> Any:
    return cbor2.loads(data, tag_hook=_tag_hook)
