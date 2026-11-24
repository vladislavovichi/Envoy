from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar, Literal

type ButtonStyle = Literal["primary", "success", "danger"]
type ContentKind = Literal["text", "service", "other"]


@dataclass(frozen=True)
class InlineButton:
    text: str
    data: str
    style: ButtonStyle | None = None


@dataclass(frozen=True)
class ReplyMarkup:
    buttons: tuple[tuple[tuple[str, str] | InlineButton, ...], ...]


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    chat_id: int
    text: str


@dataclass(frozen=True)
class TextEntity:
    type: str
    offset: int
    length: int


@dataclass(frozen=True)
class BusinessConnection:
    id: str
    owner_user_id: int
    user_chat_id: int
    rights: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    enabled: bool = True


@dataclass(frozen=True)
class MessageUpdate:
    kind: ClassVar[str] = "message"
    update_id: int
    chat_id: int
    user_id: int
    is_bot: bool = False
    message_id: int = 0
    text: str | None = None
    entities: tuple[TextEntity, ...] = ()
    content_kind: ContentKind = "text"
    edited: bool = False


@dataclass(frozen=True)
class BusinessConnectionUpdate:
    kind: ClassVar[str] = "business_connection"
    update_id: int
    connection: BusinessConnection


@dataclass(frozen=True)
class BusinessMessageUpdate:
    kind: ClassVar[str] = "business_message"
    update_id: int
    connection_id: str
    chat_id: int
    user_id: int
    is_bot: bool = False
    message_id: int = 0
    text: str | None = None
    entities: tuple[TextEntity, ...] = ()
    content_kind: ContentKind = "text"
    edited: bool = False
    done_task_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CallbackUpdate:
    kind: ClassVar[str] = "callback"
    update_id: int
    callback_id: str
    user_id: int
    chat_id: int | None
    message_id: int | None
    data: str


@dataclass(frozen=True)
class UnsupportedUpdate:
    kind: ClassVar[str] = "unsupported"
    update_id: int


type Update = (
    MessageUpdate
    | BusinessConnectionUpdate
    | BusinessMessageUpdate
    | CallbackUpdate
    | UnsupportedUpdate
)


@dataclass(frozen=True)
class SurfaceCapabilities:
    private_context: bool
    ephemeral: bool
    checklist_native: bool
