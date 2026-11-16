from dataclasses import dataclass


@dataclass(frozen=True)
class ApiFailure:
    error_code: int
    retry_after: float | None = None
