import base64
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from envoy.mandate.schema import DatetimeIssue, Mandate

MAX_SLOTS = 32
MAX_ADJUSTABLE = 8
STEP = Decimal("0.01")
REQUEST = re.compile(r"m:([0-9a-z]{1,8}):([A-Za-z0-9_-]{6}):([A-Za-z0-9_-]{0,11})")


class MandateRequestRejected(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Mandate widget request rejected: {reason}")


@dataclass(frozen=True)
class SlotOption:
    value: datetime
    label: str
    selected: bool


@dataclass(frozen=True)
class WidgetMandateView:
    revision: int
    slots: tuple[SlotOption, ...]
    adjustable_issues: tuple[str, ...]


@dataclass(frozen=True)
class MandateRequest:
    revision: int
    selected: tuple[int, ...]
    deltas: tuple[int, ...]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise MandateRequestRejected("bad_encoding") from None


def to_base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    text = ""
    while True:
        value, rest = divmod(value, 36)
        text = digits[rest] + text
        if value == 0:
            return text


def encode_request(request: MandateRequest) -> str:
    mask = sum(1 << index for index in request.selected)
    deltas = bytes(delta & 0xFF for delta in request.deltas)
    return f"m:{to_base36(request.revision)}:{_b64(mask.to_bytes(4, 'big'))}:{_b64(deltas)}"


def parse_request(data: str) -> MandateRequest:
    if len(data.encode("utf-8")) > 64:
        raise MandateRequestRejected("too_long")
    match = REQUEST.fullmatch(data)
    if match is None:
        raise MandateRequestRejected("bad_format")
    mask_bytes = _unb64(match.group(2))
    delta_bytes = _unb64(match.group(3))
    if len(mask_bytes) != 4 or len(delta_bytes) > MAX_ADJUSTABLE:
        raise MandateRequestRejected("bad_format")
    if _b64(mask_bytes) != match.group(2) or _b64(delta_bytes) != match.group(3):
        raise MandateRequestRejected("bad_encoding")
    mask = int.from_bytes(mask_bytes, "big")
    return MandateRequest(
        int(match.group(1), 36),
        tuple(index for index in range(MAX_SLOTS) if mask >> index & 1),
        tuple(byte - 256 if byte > 127 else byte for byte in delta_bytes),
    )


def project(mandate: Mandate, revision: int, label: Any) -> WidgetMandateView:
    time = next(issue for issue in mandate.issues if issue.id == "time")
    assert isinstance(time, DatetimeIssue)
    slots = tuple(
        SlotOption(value, str(label(value)), True) for value in sorted(time.choices)[:MAX_SLOTS]
    )
    issues = tuple(sorted(mandate.utility.weights))[:MAX_ADJUSTABLE]
    return WidgetMandateView(revision, slots, issues)


def apply_request(mandate: Mandate, view: WidgetMandateView, request: MandateRequest) -> Mandate:
    if request.revision != view.revision:
        raise MandateRequestRejected("stale_revision")
    if any(index >= len(view.slots) for index in request.selected):
        raise MandateRequestRejected("bad_mask")
    if not request.selected:
        raise MandateRequestRejected("no_slots")
    if len(request.deltas) != len(view.adjustable_issues):
        raise MandateRequestRejected("bad_deltas")
    weights = {key: Decimal(str(value)) for key, value in mandate.utility.weights.items()}
    for issue, delta in zip(view.adjustable_issues, request.deltas, strict=True):
        weights[issue] += STEP * delta
    if any(weight < 0 for weight in weights.values()):
        raise MandateRequestRejected("negative_weight")
    total = sum(weights.values(), Decimal(0))
    if total == 0:
        raise MandateRequestRejected("zero_weights")
    normalized = {key: float(value / total) for key, value in weights.items()}
    drift = 1.0 - sum(normalized.values())
    last = sorted(normalized)[-1]
    normalized[last] = normalized[last] + drift
    chosen = [view.slots[index].value for index in sorted(request.selected)]
    data = mandate.model_dump(mode="json")
    for issue in data["issues"]:
        if issue["id"] == "time":
            issue["choices"] = [value.isoformat() for value in chosen]
    data["utility"]["weights"] = normalized
    try:
        return Mandate.model_validate(data)
    except ValueError:
        raise MandateRequestRejected("invalid_mandate") from None


def state(view: WidgetMandateView) -> dict[str, Any]:
    return {
        "r": view.revision,
        "slots": [{"label": slot.label, "on": slot.selected} for slot in view.slots],
        "issues": list(view.adjustable_issues),
    }
