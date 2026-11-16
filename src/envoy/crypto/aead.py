from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from envoy.domain.errors import DecryptionFailed
from envoy.ports.rng import Rng


class ContentCipher:
    def __init__(self, rng: Rng) -> None:
        self._rng = rng

    def generate_key(self) -> bytes:
        return self._rng.token_bytes(32)

    def encrypt(self, key: bytes, plaintext: bytes, aad: bytes) -> bytes:
        nonce = self._rng.token_bytes(12)
        return nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)

    def decrypt(self, key: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        try:
            return ChaCha20Poly1305(key).decrypt(ciphertext[:12], ciphertext[12:], aad)
        except (InvalidTag, ValueError) as exc:
            raise DecryptionFailed("Content cannot be decrypted") from exc
