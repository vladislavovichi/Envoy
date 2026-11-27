from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RecordedCall:
    method: str
    params: dict[str, Any]
    token: str = ""
