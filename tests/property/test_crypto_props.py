from hypothesis import given, settings
from hypothesis import strategies as st

from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.ports.rng import SeededRng


@settings(max_examples=30)
@given(st.binary(max_size=1_048_576))
def test_cipher_roundtrip_any_bytes(payload: bytes) -> None:
    cipher = ContentCipher(SeededRng(5))
    key = cipher.generate_key()
    assert cipher.decrypt(key, cipher.encrypt(key, payload, b"aad"), b"aad") == payload


@given(st.binary(min_size=1, max_size=1024), st.integers(min_value=0, max_value=1023))
def test_signature_fails_on_any_single_byte_flip(payload: bytes, index: int) -> None:
    key = SigningKey.generate()
    signature = key.sign(payload)
    changed = bytearray(payload)
    changed[index % len(payload)] ^= 1
    assert not key.public().verify(bytes(changed), signature)
