import random
import secrets
from typing import Protocol


class Rng(Protocol):
    def token_bytes(self, n: int) -> bytes: ...


class SystemRng:
    def token_bytes(self, n: int) -> bytes:
        return secrets.token_bytes(n)


class SeededRng:
    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def token_bytes(self, n: int) -> bytes:
        return self._rng.randbytes(n)
