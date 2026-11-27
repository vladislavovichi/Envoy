from datetime import UTC, datetime
from typing import Any

from envoy.domain.telegram import (
    BotMessageUpdate,
    BusinessConnection,
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    ContentKind,
    GuestMessageUpdate,
    InlineQueryUpdate,
    ManagedBotUpdate,
    MessageUpdate,
    TextEntity,
    UnsupportedUpdate,
    Update,
    WidgetQueryUpdate,
)

ALLOWED_UPDATES = (
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "widget_query",
    "guest_message",
    "inline_query",
    "managed_bot",
)
SENSITIVE_ENTITIES = frozenset({"bank_card_number", "ton_address"})
SERVICE_FIELDS = frozenset(
    {
        "checklist_tasks_done",
        "checklist_tasks_added",
        "paid_messages_waived",
        "paid_message_price_changed",
        "new_chat_members",
        "left_chat_member",
        "pinned_message",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
        "write_access_allowed",
    }
)


def _content_kind(message: dict[str, Any]) -> ContentKind:
    if isinstance(message.get("text"), str):
        return "text"
    if SERVICE_FIELDS & message.keys():
        return "service"
    return "other"


def _entities(message: dict[str, Any]) -> tuple[TextEntity, ...]:
    return tuple(
        TextEntity(str(entity["type"]), int(entity["offset"]), int(entity["length"]))
        for entity in message.get("entities", ())
        if entity.get("type") in SENSITIVE_ENTITIES
    )


def _message_fields(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("from") or {}
    text = message.get("text")
    return {
        "chat_id": int(message["chat"]["id"]),
        "user_id": int(sender.get("id", 0)),
        "is_bot": bool(sender.get("is_bot")) or "sender_business_bot" in message,
        "message_id": int(message.get("message_id", 0)),
        "text": text if isinstance(text, str) else None,
        "entities": _entities(message),
        "content_kind": _content_kind(message),
    }


def _widget_query(update_id: int, query: dict[str, Any], received_at: datetime) -> Update:
    chat = query.get("chat") or {}
    kind = query.get("type") or {}
    data = query.get("data")
    return WidgetQueryUpdate(
        update_id=update_id,
        query_id=str(query["id"]),
        user_id=int(query["from"]["id"]),
        chat_id=int(chat["id"]) if "id" in chat else None,
        message_id=int(query["message_id"]) if "message_id" in query else None,
        data=data if kind.get("type") == "data" and isinstance(data, str) else None,
        received_at=received_at,
    )


def _map(raw: dict[str, Any], received_at: datetime) -> Update:
    update_id = int(raw["update_id"])
    managed = raw.get("managed_bot")
    if isinstance(managed, dict):
        return ManagedBotUpdate(
            update_id=update_id,
            user_id=int(managed["user"]["id"]),
            bot_id=int(managed["bot"]["id"]),
            bot_username=str(managed["bot"].get("username", "")),
        )
    if isinstance(raw.get("message"), dict) and isinstance(
        raw["message"].get("managed_bot_created"), dict
    ):
        created = raw["message"]["managed_bot_created"]["bot"]
        return ManagedBotUpdate(
            update_id=update_id,
            user_id=int(raw["message"]["from"]["id"]),
            bot_id=int(created["id"]),
            bot_username=str(created.get("username", "")),
        )
    if isinstance(raw.get("message"), dict):
        message = raw["message"]
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if sender.get("is_bot") and chat.get("type") == "private" and isinstance(text, str):
            return BotMessageUpdate(
                bot_id=0,
                update_id=update_id,
                from_bot_id=int(sender["id"]),
                chat_id=int(chat["id"]),
                text=text,
                received_at=received_at,
            )
        return MessageUpdate(update_id=update_id, **_message_fields(message))
    for key, edited in (("business_message", False), ("edited_business_message", True)):
        message = raw.get(key)
        if isinstance(message, dict):
            done = (message.get("checklist_tasks_done") or {}).get("marked_as_done_task_ids", ())
            return BusinessMessageUpdate(
                update_id=update_id,
                connection_id=str(message["business_connection_id"]),
                edited=edited,
                done_task_ids=tuple(int(task) for task in done),
                **_message_fields(message),
            )
    callback = raw.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        return CallbackUpdate(
            update_id=update_id,
            callback_id=str(callback["id"]),
            user_id=int(callback["from"]["id"]),
            chat_id=int(chat["id"]) if "id" in chat else None,
            message_id=int(message["message_id"]) if "message_id" in message else None,
            data=str(callback.get("data", "")),
        )
    query = raw.get("widget_query")
    if isinstance(query, dict):
        return _widget_query(update_id, query, received_at)
    guest = raw.get("guest_message")
    if isinstance(guest, dict) and guest.get("guest_query_id"):
        text = guest.get("text")
        return GuestMessageUpdate(
            update_id=update_id,
            guest_query_id=str(guest["guest_query_id"]),
            user_id=int((guest.get("from") or {})["id"]),
            chat_id=int(guest["chat"]["id"]),
            text=text if isinstance(text, str) else None,
        )
    inline = raw.get("inline_query")
    if isinstance(inline, dict):
        return InlineQueryUpdate(
            update_id=update_id,
            query_id=str(inline["id"]),
            user_id=int(inline["from"]["id"]),
            query=str(inline.get("query", ""))[:256],
        )
    connection = raw.get("business_connection")
    if isinstance(connection, dict):
        rights = connection.get("rights") or {}
        return BusinessConnectionUpdate(
            update_id,
            BusinessConnection(
                id=str(connection["id"]),
                owner_user_id=int(connection["user"]["id"]),
                user_chat_id=int(connection["user_chat_id"]),
                rights={str(key): bool(value) for key, value in rights.items()},
                enabled=bool(connection.get("is_enabled", False)),
            ),
        )
    return UnsupportedUpdate(update_id)


def map_update(raw: dict[str, Any], received_at: datetime | None = None) -> Update:
    try:
        return _map(raw, received_at or datetime.now(UTC))
    except KeyError, TypeError, ValueError, AttributeError:
        return UnsupportedUpdate(int(raw.get("update_id", 0)))
