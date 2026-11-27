import base64
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from envoy.crypto.keys import SigningKey
from envoy.domain.ids import SessionId
from envoy.protocol.envelope import (
    MESSAGE_TYPES,
    WIRE_VERSION,
    DecodedEnvelope,
    Envelope,
    InvalidEnvelope,
    decode_message,
    encode_message,
)

KEY = SigningKey.from_bytes(bytes(range(32)))


def outcome(text: str) -> DecodedEnvelope | InvalidEnvelope:
    try:
        return decode_message(text)
    except InvalidEnvelope as error:
        return error


@settings(max_examples=300)
@given(st.text(max_size=5000))
def test_arbitrary_text_yields_dto_or_invalid(text: str) -> None:
    assert isinstance(outcome(text), (DecodedEnvelope, InvalidEnvelope))


@settings(max_examples=300)
@given(st.binary(max_size=3000))
def test_arbitrary_payload_yields_dto_or_invalid(raw: bytes) -> None:
    text = "envoy/1:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert isinstance(outcome(text), (DecodedEnvelope, InvalidEnvelope))


scalars = st.one_of(
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.text(max_size=20),
    st.binary(max_size=20),
    st.booleans(),
    st.none(),
)
bodies = st.dictionaries(st.text(min_size=1, max_size=8), scalars, max_size=6)


@settings(max_examples=150)
@given(
    kind=st.sampled_from(sorted(MESSAGE_TYPES)),
    body=bodies,
    seq=st.integers(min_value=1, max_value=2**40),
    ack=st.integers(min_value=0, max_value=2**40),
    ts=st.integers(min_value=0, max_value=2**40),
    ttl=st.integers(min_value=0, max_value=3600),
    nonce=st.binary(min_size=16, max_size=16),
    extra=st.dictionaries(st.from_regex(r"x-[a-z]{1,6}", fullmatch=True), scalars, max_size=3),
    random=st.integers(min_value=0, max_value=2**62 - 1),
)
def test_encoded_envelopes_round_trip_and_verify(
    kind, body, seq, ack, ts, ttl, nonce, extra, random
) -> None:
    sid = SessionId(UUID(int=(0x7 << 76) | (0x2 << 62) | random))
    message = Envelope(
        v=WIRE_VERSION,
        sid=sid,
        seq=seq,
        ack=ack,
        type=kind,
        body=body,
        ts=ts,
        exp=ts + ttl,
        nonce=nonce,
        extra=extra,
    )
    decoded = decode_message(encode_message(message, KEY))
    assert decoded.verify(KEY.public())
    assert decoded.envelope.type == kind
    assert dict(decoded.envelope.body) == body
    assert dict(decoded.envelope.extra) == extra
    assert (decoded.envelope.seq, decoded.envelope.ack, decoded.envelope.sid) == (seq, ack, sid)
