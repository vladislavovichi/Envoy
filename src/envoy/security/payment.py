import re
from collections.abc import Iterable, Sequence

from stdnum import luhn

from envoy.domain.telegram import TextEntity

CARD_LABEL = "[карта]"
TON_LABEL = "[TON-адрес]"
DIGIT_RUN = re.compile(r"\d(?:[\d -]*\d)?")
TON_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])[EUk0]Q[A-Za-z0-9_-]{46}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![0-9A-Za-z:-])(?:-1|0):[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
)
ENTITY_LABELS = {"bank_card_number": CARD_LABEL, "ton_address": TON_LABEL}

type Span = tuple[int, int, str]


def card_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for run in DIGIT_RUN.finditer(text):
        groups = list(re.finditer(r"\d+", run.group()))
        for index, first in enumerate(groups):
            digits = ""
            for last in groups[index:]:
                digits += last.group()
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and luhn.is_valid(digits):
                    spans.append((run.start() + first.start(), run.start() + last.end()))
    return merge((start, end, "") for start, end in spans)


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end, label in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(end, previous[1]), previous[2])
        else:
            merged.append((start, end, label))
    return merged


def merge(spans: Iterable[Span]) -> list[tuple[int, int]]:
    return [(start, end) for start, end, _ in merge_spans(spans)]


def utf16_to_index(text: str, offset: int) -> int:
    units = 0
    for index, character in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
    return len(text)


def entity_spans(text: str, entities: Sequence[TextEntity]) -> list[Span]:
    spans: list[Span] = []
    for entity in entities:
        label = ENTITY_LABELS.get(entity.type)
        if label is None:
            continue
        start = utf16_to_index(text, entity.offset)
        end = utf16_to_index(text, entity.offset + entity.length)
        if start < end:
            spans.append((start, end, label))
    return spans


def payment_spans(text: str, entities: Sequence[TextEntity] = ()) -> list[Span]:
    spans: list[Span] = [(start, end, CARD_LABEL) for start, end in card_spans(text)]
    for pattern in TON_PATTERNS:
        spans += [(match.start(), match.end(), TON_LABEL) for match in pattern.finditer(text)]
    spans += entity_spans(text, entities)
    return merge_spans(spans)


def redact_payment_details(text: str, entities: Sequence[TextEntity] = ()) -> tuple[str, bool]:
    spans = payment_spans(text, entities)
    pieces: list[str] = []
    offset = 0
    for start, end, label in spans:
        pieces += [text[offset:start], label]
        offset = end
    return "".join(pieces) + text[offset:], bool(spans)
