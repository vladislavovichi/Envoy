from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest

from envoy.mandate.schema import Mandate
from envoy.ports.rng import Rng


@dataclass(frozen=True)
class MandateCommitment:
    salt: bytes
    digest: bytes


def commitment_digest(mandate: Mandate, salt: bytes) -> bytes:
    return sha256(salt + mandate.canonical_bytes()).digest()


def commit(mandate: Mandate, rng: Rng) -> MandateCommitment:
    salt = rng.token_bytes(32)
    return MandateCommitment(salt, commitment_digest(mandate, salt))


def verify_reveal(digest: bytes, mandate: Mandate, salt: bytes) -> bool:
    return compare_digest(digest, commitment_digest(mandate, salt))


def attestation_bytes(mandate: Mandate) -> bytes:
    return mandate.canonical_bytes() + mandate.version.to_bytes(4, "big")
