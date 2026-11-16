import pytest

from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import PublicKey, SigningKey
from envoy.domain.errors import DecryptionFailed
from envoy.ports.rng import SeededRng


def test_sign_verify_roundtrip() -> None:
    key = SigningKey.generate()
    restored = SigningKey.from_bytes(key.to_bytes())
    public = PublicKey.from_bytes(restored.public().to_bytes())
    assert public.verify(b"hello", key.sign(b"hello"))


def test_verify_rejects_modified_data() -> None:
    key = SigningKey.generate()
    assert not key.public().verify(b"other", key.sign(b"hello"))
    assert not key.public().verify(b"hello", b"short")


def test_verify_rejects_other_key() -> None:
    assert not SigningKey.generate().public().verify(b"hello", SigningKey.generate().sign(b"hello"))


def test_fingerprint_stable_and_16_chars() -> None:
    public = SigningKey.generate().public()
    assert public.fingerprint() == PublicKey.from_bytes(public.to_bytes()).fingerprint()
    assert len(public.fingerprint()) == 16


@pytest.mark.parametrize("payload", [b"", b"hello", bytes(1_048_576)])
def test_cipher_roundtrip(payload: bytes) -> None:
    cipher = ContentCipher(SeededRng(5))
    key = cipher.generate_key()
    ciphertext = cipher.encrypt(key, payload, b"session")
    assert cipher.decrypt(key, ciphertext, b"session") == payload
    assert ciphertext != payload


@pytest.mark.parametrize("failure", ["aad", "key", "truncated", "modified"])
def test_cipher_rejects_wrong_aad(failure: str) -> None:
    cipher = ContentCipher(SeededRng(1))
    key = cipher.generate_key()
    encrypted = cipher.encrypt(key, b"payload", b"aad")
    if failure == "key":
        key = cipher.generate_key()
    if failure == "truncated":
        encrypted = b"x"
    if failure == "modified":
        encrypted = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(DecryptionFailed):
        cipher.decrypt(key, encrypted, b"wrong" if failure == "aad" else b"aad")
