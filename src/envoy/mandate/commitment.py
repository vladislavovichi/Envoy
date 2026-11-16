from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest

from envoy.mandate.schema import Mandate
from envoy.ports.rng import Rng


@dataclass(frozen=True)
class MandateCommitment:
    salt: bytes
    digest: bytes


def commit(mandate: Mandate, rng: Rng) -> MandateCommitment:
    salt = rng.token_bytes(32)
    return MandateCommitment(salt, sha256(salt + mandate.canonical_bytes()).digest())


def verify_reveal(commitment_digest: bytes, mandate: Mandate, salt: bytes) -> bool:
    return compare_digest(commitment_digest, sha256(salt + mandate.canonical_bytes()).digest())
