import base64
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import cbor2
import pytest

from envoy.crypto.keys import SigningKey
from envoy.domain.canonical import encode
from envoy.domain.ids import SessionId
from envoy.domain.offer import Offer
from envoy.protocol.bodies import (
    AcceptBody,
    CapabilitiesBody,
    CommitBody,
    HelloBody,
    ProposeBody,
    offer_body,
    parse_body,
    version_map,
)
from envoy.protocol.envelope import (
    MAX_BODY,
    MAX_TEXT,
    WIRE_VERSION,
    Envelope,
    InvalidEnvelope,
    Version,
    decode_message,
    encode_message,
    unknown_critical,
)

KEY = SigningKey.from_bytes(bytes(range(32)))
OTHER = SigningKey.from_bytes(bytes(range(1, 33)))
SID = SessionId(UUID("01936d1c-7b5a-7c3e-8f00-000000000001"))
NONCE = bytes(range(16))
OFFER = Offer(values={"time": datetime(2026, 12, 3, 10, tzinfo=UTC), "place": "кафе"})


def envelope(**changes) -> Envelope:
    values = {
        "v": WIRE_VERSION,
        "sid": SID,
        "seq": 1,
        "ack": 0,
        "type": "propose",
        "body": offer_body(OFFER, 1),
        "ts": 1_764_000_000,
        "exp": 1_764_000_060,
        "nonce": NONCE,
    }
    values.update(changes)
    return Envelope(**values)


def reencode(fields: dict) -> str:
    raw = encode(fields)
    return "envoy/1:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_vector_prefix_fields_and_signature() -> None:
    text = encode_message(envelope(), KEY)
    assert text.startswith("envoy/1:")
    assert text == encode_message(envelope(), KEY)
    decoded = decode_message(text)
    message = decoded.envelope
    assert (message.v, message.sid, message.seq, message.ack) == (Version(1, 0), SID, 1, 0)
    assert message.sid.version == 7
    assert message.nonce == NONCE
    assert len(message.sig) == 64
    assert decoded.verify(KEY.public())
    assert not decoded.verify(OTHER.public())
    assert decoded.raw == encode(dict(decoded.fields))


def test_key_order_does_not_change_bytes() -> None:
    body = offer_body(OFFER, 1)
    reordered = dict(reversed(list(body.items())))
    assert encode_message(envelope(body=reordered), KEY) == encode_message(envelope(), KEY)


def test_unknown_optional_field_is_signed_preserved_and_tamper_evident() -> None:
    text = encode_message(envelope(extra={"x-note": "привет"}), KEY)
    decoded = decode_message(text)
    assert decoded.verify(KEY.public())
    assert decoded.envelope.extra == {"x-note": "привет"}
    tampered = dict(decoded.fields)
    tampered["x-note"] = "пока"
    assert not decode_message(reencode(tampered)).verify(KEY.public())
    dropped = {key: value for key, value in decoded.fields.items() if key != "x-note"}
    assert not decode_message(reencode(dropped)).verify(KEY.public())


def test_signed_body_change_fails_verification() -> None:
    decoded = decode_message(encode_message(envelope(), KEY))
    fields = dict(decoded.fields)
    fields["seq"] = 2
    assert not decode_message(reencode(fields)).verify(KEY.public())


def test_unknown_critical_fields_are_reported() -> None:
    message = decode_message(encode_message(envelope(crit=("x-psi", "x-note")), KEY)).envelope
    assert unknown_critical(message, frozenset({"x-note"})) == ("x-psi",)
    assert unknown_critical(message, frozenset({"x-note", "x-psi"})) == ()


def padded_body(size: int) -> dict:
    for length in range(size, 0, -1):
        body = {"pad": bytes(length)}
        if len(encode(body)) == size:
            return body
    raise AssertionError("no body of the requested size")


def test_body_limit_2048_and_2049() -> None:
    encode_message(envelope(type="ping", body=padded_body(MAX_BODY)), KEY)
    with pytest.raises(InvalidEnvelope, match="body_too_large"):
        encode_message(envelope(type="ping", body=padded_body(MAX_BODY + 1)), KEY)
    oversized = dict(decode_message(encode_message(envelope(type="ping", body={}), KEY)).fields)
    oversized["body"] = padded_body(MAX_BODY + 1)
    with pytest.raises(InvalidEnvelope, match="body_too_large"):
        decode_message(reencode(oversized))


def test_text_limit_4096_and_4097() -> None:
    lengths = {}
    reasons = set()
    for size in range(2860, 2920):
        try:
            text = encode_message(envelope(type="ping", body={}, extra={"x": bytes(size)}), KEY)
        except InvalidEnvelope as error:
            reasons.add(error.reason)
            continue
        lengths[len(text)] = text
    assert reasons <= {"message_too_long"}
    assert max(lengths) <= MAX_TEXT
    assert MAX_TEXT in lengths
    assert decode_message(lengths[MAX_TEXT]).verify(KEY.public())
    with pytest.raises(InvalidEnvelope, match="message_too_long"):
        decode_message(lengths[MAX_TEXT] + "A")
    with pytest.raises(InvalidEnvelope, match="message_too_long"):
        encode_message(envelope(type="ping", body={}, extra={"x": bytes(2950)}), KEY)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda text: text[:-5], None),
        (lambda text: "envoy/2:" + text[8:], "bad_version"),
        (lambda text: "envoy/01:" + text[8:], "bad_prefix"),
        (lambda text: "Envoy/1:" + text[8:], "bad_prefix"),
        (lambda text: text + "=", "bad_prefix"),
        (lambda text: text + "A", None),
        (lambda text: text[:-1] + "+", "bad_prefix"),
        (lambda text: "envoy/1:", "bad_cbor"),
    ],
)
def test_malformed_text_is_rejected(mutate, reason) -> None:
    text = encode_message(envelope(), KEY)
    with pytest.raises(InvalidEnvelope) as error:
        decode_message(mutate(text))
    if reason is not None:
        assert error.value.reason == reason


def raw_text(raw: bytes) -> str:
    return "envoy/1:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_trailing_bytes_and_non_deterministic_cbor_are_rejected() -> None:
    decoded = decode_message(encode_message(envelope(), KEY))
    with pytest.raises(InvalidEnvelope, match="trailing_bytes"):
        decode_message(raw_text(decoded.raw + b"\x00"))
    fields = dict(decoded.fields)
    reordered = dict(sorted(fields.items(), reverse=True))
    loose = cbor2.dumps({key: value for key, value in reordered.items()}, canonical=False)
    assert loose != decoded.raw
    with pytest.raises(InvalidEnvelope, match="non_canonical"):
        decode_message(raw_text(loose))
    indefinite = b"\xbf" + decoded.raw[1:] + b"\xff"
    with pytest.raises(InvalidEnvelope):
        decode_message(raw_text(indefinite))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sid", UUID(int=5), "bad_sid"),
        ("nonce", bytes(15), "bad_nonce"),
        ("seq", 0, "bad_field"),
        ("ack", -1, "bad_field"),
        ("type", "teleport", "unknown_type"),
        ("v", [1], "bad_version"),
        ("exp", 1, "bad_expiry"),
        ("sig", bytes(63), "bad_signature"),
        ("crit", "x", "bad_crit"),
    ],
)
def test_field_validation(field, value, reason) -> None:
    fields = dict(decode_message(encode_message(envelope(), KEY)).fields)
    fields[field] = value
    with pytest.raises(InvalidEnvelope) as error:
        decode_message(reencode(fields))
    assert error.value.reason == reason


def test_missing_signature_and_non_map_are_rejected() -> None:
    fields = dict(decode_message(encode_message(envelope(), KEY)).fields)
    del fields["sig"]
    with pytest.raises(InvalidEnvelope, match="bad_signature"):
        decode_message(reencode(fields))
    with pytest.raises(InvalidEnvelope, match="not_a_map"):
        decode_message(raw_text(encode([1, 2])))


def test_offer_body_types_and_hash_binding() -> None:
    decoded = decode_message(encode_message(envelope(), KEY))
    body = parse_body("propose", decoded.envelope.body)
    assert isinstance(body, ProposeBody)
    assert body.public_offer() == OFFER
    assert body.offer_hash == OFFER.hash()
    forged = dict(decoded.envelope.body)
    forged["offer"] = {**forged["offer"], "place": "вокзал"}
    with pytest.raises(InvalidEnvelope, match="bad_body"):
        parse_body("propose", forged)
    decimal = Offer(values={"price": Decimal("10.50")})
    parsed = parse_body("counter", offer_body(decimal, 2, OFFER.hash()))
    assert parsed.public_offer() == decimal


def test_hello_capabilities_and_commit_bodies() -> None:
    hello = parse_body(
        "hello",
        {
            "agent_id": bytes(16),
            "owner_id": bytes(16),
            "min_version": version_map(Version(1, 0)),
            "max_version": version_map(Version(1, 2)),
            "token": None,
            "future": True,
        },
    )
    assert isinstance(hello, HelloBody)
    with pytest.raises(InvalidEnvelope):
        parse_body(
            "hello",
            {
                "agent_id": bytes(16),
                "owner_id": bytes(16),
                "min_version": version_map(Version(2, 0)),
                "max_version": version_map(Version(1, 0)),
            },
        )
    capabilities = parse_body("capabilities", {"items": ["issue:datetime@1", "deposit:stars@2"]})
    assert isinstance(capabilities, CapabilitiesBody)
    with pytest.raises(InvalidEnvelope):
        parse_body("capabilities", {"items": ["issue:datetime"]})
    assert isinstance(parse_body("accept", {"offer_hash": bytes(32)}), AcceptBody)
    ready = parse_body("commit", {"stage": "ready", "offer_hash": bytes(32)})
    assert isinstance(ready, CommitBody)
    assert ready.decision is None
    decision = parse_body(
        "commit", {"stage": "decision", "offer_hash": bytes(32), "decision": "commit"}
    )
    assert decision.decision == "commit"
    for invalid in (
        {"stage": "ready", "offer_hash": bytes(32), "decision": "commit"},
        {"stage": "decision", "offer_hash": bytes(32)},
        {"stage": "decision", "offer_hash": bytes(32), "decision": "maybe"},
        {"stage": "ready", "offer_hash": bytes(31)},
    ):
        with pytest.raises(InvalidEnvelope):
            parse_body("commit", invalid)
    with pytest.raises(InvalidEnvelope):
        parse_body("accept", {"offer_hash": "00" * 32})


def test_extra_cannot_shadow_known_field() -> None:
    with pytest.raises(InvalidEnvelope, match="extra_shadows_field"):
        encode_message(envelope(extra={"seq": 9}), KEY)
