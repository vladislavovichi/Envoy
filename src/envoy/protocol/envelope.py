import base64
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast, get_args
from uuid import UUID

import cbor2

from envoy.crypto.keys import PublicKey, SigningKey
from envoy.domain.canonical import decode, encode
from envoy.domain.ids import SessionId

type MessageType = Literal[
    "hello",
    "capabilities",
    "propose",
    "counter",
    "accept",
    "reject",
    "commit",
    "revoke",
    "dispute",
    "revoke_key",
    "rotate_key",
    "decision_request",
    "decision",
    "ack",
    "ping",
    "error",
]

MESSAGE_TYPES: frozenset[str] = frozenset(get_args(MessageType.__value__))
SERVICE_TYPES: frozenset[str] = frozenset({"ack", "ping", "error"})
PREFIX = "envoy/"
MAX_TEXT = 4096
MAX_BODY = 2048
NONCE_SIZE = 16
SID_VERSIONS = frozenset({5, 7})
SIGNATURE_SIZE = 64
KNOWN_FIELDS = frozenset(
    {"v", "sid", "seq", "ack", "type", "body", "ts", "exp", "nonce", "chain", "crit", "sig"}
)
PREFIX_PATTERN = re.compile(r"envoy/(0|[1-9][0-9]{0,3}):([A-Za-z0-9_-]*)")


class InvalidEnvelope(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid protocol envelope: {reason}")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


WIRE_VERSION = Version(1, 0)


@dataclass(frozen=True)
class Envelope:
    v: Version
    sid: SessionId
    seq: int
    ack: int
    type: MessageType
    body: Mapping[str, Any]
    ts: int
    exp: int
    nonce: bytes
    chain: Mapping[str, Any] | None = None
    crit: tuple[str, ...] = ()
    sig: bytes = b""
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


def _fields(envelope: Envelope) -> dict[str, Any]:
    fields: dict[str, Any] = dict(envelope.extra)
    fields.update(
        {
            "v": [envelope.v.major, envelope.v.minor],
            "sid": envelope.sid,
            "seq": envelope.seq,
            "ack": envelope.ack,
            "type": envelope.type,
            "body": dict(envelope.body),
            "ts": envelope.ts,
            "exp": envelope.exp,
            "nonce": envelope.nonce,
            "chain": None if envelope.chain is None else dict(envelope.chain),
            "crit": list(envelope.crit),
        }
    )
    return fields


def signed_bytes(fields: Mapping[str, Any]) -> bytes:
    return encode({key: value for key, value in fields.items() if key != "sig"})


def body_size(body: Mapping[str, Any]) -> int:
    return len(encode(dict(body)))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encode_message(envelope: Envelope, signer: SigningKey) -> str:
    if KNOWN_FIELDS & envelope.extra.keys():
        raise InvalidEnvelope("extra_shadows_field")
    if body_size(envelope.body) > MAX_BODY:
        raise InvalidEnvelope("body_too_large")
    fields = _fields(envelope)
    _validate(fields)
    fields["sig"] = signer.sign(signed_bytes(fields))
    text = f"{PREFIX}{envelope.v.major}:{_b64(encode(fields))}"
    if len(text) > MAX_TEXT:
        raise InvalidEnvelope("message_too_long")
    return text


@dataclass(frozen=True)
class DecodedEnvelope:
    raw: bytes
    fields: Mapping[str, Any]
    envelope: Envelope

    def signed_bytes(self) -> bytes:
        return signed_bytes(self.fields)

    def verify(self, key: PublicKey) -> bool:
        return key.verify(self.signed_bytes(), self.envelope.sig)


def _payload(text: str) -> tuple[int, bytes]:
    if len(text) > MAX_TEXT:
        raise InvalidEnvelope("message_too_long")
    match = PREFIX_PATTERN.fullmatch(text)
    if match is None:
        raise InvalidEnvelope("bad_prefix")
    encoded = match.group(2)
    if len(encoded) % 4 == 1:
        raise InvalidEnvelope("bad_base64")
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if _b64(raw) != encoded:
        raise InvalidEnvelope("bad_base64")
    return int(match.group(1)), raw


def _single_item(raw: bytes) -> Any:
    stream = io.BytesIO(raw)
    try:
        cbor2.CBORDecoder(stream).decode()
    except cbor2.CBORDecodeError, ValueError, TypeError, OverflowError, RecursionError:
        raise InvalidEnvelope("bad_cbor") from None
    if stream.tell() != len(raw):
        raise InvalidEnvelope("trailing_bytes")
    try:
        value = decode(raw)
        canonical = encode(value)
    except cbor2.CBORError, ValueError, TypeError, OverflowError, RecursionError:
        raise InvalidEnvelope("bad_cbor") from None
    if canonical != raw:
        raise InvalidEnvelope("non_canonical")
    return value


def _int(value: Any, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise InvalidEnvelope("bad_field")
    return value


def _validate(fields: Mapping[str, Any]) -> Envelope:
    missing = (KNOWN_FIELDS - {"sig"}) - fields.keys()
    if missing:
        raise InvalidEnvelope("missing_field")
    if not all(isinstance(key, str) for key in fields):
        raise InvalidEnvelope("bad_field")
    version = fields["v"]
    if (
        not isinstance(version, list)
        or len(version) != 2
        or not all(isinstance(part, int) and not isinstance(part, bool) for part in version)
        or min(version) < 0
    ):
        raise InvalidEnvelope("bad_version")
    sid = fields["sid"]
    if not isinstance(sid, UUID) or sid.version not in SID_VERSIONS:
        raise InvalidEnvelope("bad_sid")
    kind = fields["type"]
    if kind not in MESSAGE_TYPES:
        raise InvalidEnvelope("unknown_type")
    body = fields["body"]
    if not isinstance(body, dict) or not all(isinstance(key, str) for key in body):
        raise InvalidEnvelope("bad_body")
    if body_size(body) > MAX_BODY:
        raise InvalidEnvelope("body_too_large")
    nonce = fields["nonce"]
    if not isinstance(nonce, bytes) or len(nonce) != NONCE_SIZE:
        raise InvalidEnvelope("bad_nonce")
    chain = fields["chain"]
    if chain is not None and (
        not isinstance(chain, dict) or not all(isinstance(key, str) for key in chain)
    ):
        raise InvalidEnvelope("bad_chain")
    crit = fields["crit"]
    if not isinstance(crit, list) or not all(isinstance(item, str) for item in crit):
        raise InvalidEnvelope("bad_crit")
    ts = _int(fields["ts"], 0)
    exp = _int(fields["exp"], 0)
    if exp < ts:
        raise InvalidEnvelope("bad_expiry")
    signature = fields.get("sig", b"")
    if "sig" in fields and (not isinstance(signature, bytes) or len(signature) != SIGNATURE_SIZE):
        raise InvalidEnvelope("bad_signature")
    return Envelope(
        v=Version(version[0], version[1]),
        sid=SessionId(sid),
        seq=_int(fields["seq"], 1),
        ack=_int(fields["ack"], 0),
        type=cast(MessageType, kind),
        body=MappingProxyType(body),
        ts=ts,
        exp=exp,
        nonce=nonce,
        chain=None if chain is None else MappingProxyType(chain),
        crit=tuple(crit),
        sig=signature,
        extra=MappingProxyType(
            {key: value for key, value in fields.items() if key not in KNOWN_FIELDS}
        ),
    )


def decode_message(text: str) -> DecodedEnvelope:
    if not isinstance(text, str):
        raise InvalidEnvelope("bad_prefix")
    major, raw = _payload(text)
    fields = _single_item(raw)
    if not isinstance(fields, dict):
        raise InvalidEnvelope("not_a_map")
    if "sig" not in fields:
        raise InvalidEnvelope("bad_signature")
    envelope = _validate(fields)
    if envelope.v.major != major:
        raise InvalidEnvelope("bad_version")
    return DecodedEnvelope(raw, MappingProxyType(fields), envelope)


def unknown_critical(envelope: Envelope, understood: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(item for item in envelope.crit if item not in understood))
