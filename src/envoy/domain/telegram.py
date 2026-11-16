from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyMarkup:
    buttons: tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    chat_id: int
    text: str


@dataclass(frozen=True)
class Update:
    update_id: int
    chat_id: int | None = None
    text: str | None = None
    ignored: bool = False


@dataclass(frozen=True)
class SurfaceCapabilities:
    private_context: bool
    ephemeral: bool
    checklist_native: bool
