from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class PublicKey:
    def __init__(self, key: Ed25519PublicKey) -> None:
        self._key = key

    def verify(self, data: bytes, signature: bytes) -> bool:
        try:
            self._key.verify(signature, data)
        except InvalidSignature, ValueError:
            return False
        return True

    def fingerprint(self) -> str:
        return sha256(self.to_bytes()).hexdigest()[:16]

    def to_bytes(self) -> bytes:
        return self._key.public_bytes_raw()

    @classmethod
    def from_bytes(cls, data: bytes) -> PublicKey:
        return cls(Ed25519PublicKey.from_public_bytes(data))


class SigningKey:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate())

    def public(self) -> PublicKey:
        return PublicKey(self._key.public_key())

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def to_bytes(self) -> bytes:
        return self._key.private_bytes_raw()

    @classmethod
    def from_bytes(cls, data: bytes) -> SigningKey:
        return cls(Ed25519PrivateKey.from_private_bytes(data))
