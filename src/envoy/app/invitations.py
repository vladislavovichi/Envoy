import asyncio
import base64
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

import structlog

from envoy.app.ports import SessionRecord
from envoy.domain.ids import SessionId
from envoy.domain.telegram import GuestMessageUpdate, InlineQueryUpdate, MessageUpdate
from envoy.ports.clock import Clock
from envoy.ports.rng import Rng
from envoy.telegram.ports import BotIdentity, InviteSurface, MessageSender, SendRejected

logger = structlog.get_logger()
HUMAN = "human"
AGENT_PREFIX = "agent:"
CARD_TEXT = (
    "Это Envoy — ИИ-агент, который договаривается от имени владельца аккаунта. "
    "Итог подтверждает человек."
)


class InvitationRejected(PermissionError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invitation rejected: {reason}")


@dataclass(frozen=True)
class Invitation:
    token_hash: bytes
    sid: SessionId
    counterpart_user_id: int | None
    expires_at: datetime
    used_at: datetime | None = None


@dataclass(frozen=True)
class VerifiedPeer:
    bot_id: int
    user_id: int


def token_hash(token: str) -> bytes:
    return sha256(token.encode("utf-8")).digest()


def sid_of(token: str) -> SessionId | None:
    try:
        return SessionId(UUID(hex=token[:32]))
    except ValueError:
        return None


def agent_route(bot_id: int) -> str:
    return f"{AGENT_PREFIX}{bot_id}"


def check_claim(
    invitation: Invitation | None, now: datetime, expected_user: int | None
) -> Invitation:
    if invitation is None:
        raise InvitationRejected("unknown_token")
    if invitation.used_at is not None:
        raise InvitationRejected("token_used")
    if now >= invitation.expires_at:
        raise InvitationRejected("token_expired")
    if invitation.counterpart_user_id is not None and expected_user is not None:
        if invitation.counterpart_user_id != expected_user:
            raise InvitationRejected("wrong_user")
    return invitation


class InvitationRepository(Protocol):
    async def issue(self, invitation: Invitation) -> None: ...
    async def get(self, hash: bytes) -> Invitation | None: ...
    async def claim(
        self, hash: bytes, now: datetime, route: str, expected_user: int | None
    ) -> Invitation: ...
    async def route(self, sid: SessionId) -> str | None: ...
    async def switch(self, sid: SessionId, expected: str, route: str) -> bool: ...
    async def authorize(self, user_id: int, bot_id: int) -> None: ...
    async def authorized(self, user_id: int, bot_id: int) -> bool: ...


class InMemoryInvitationRepository:
    def __init__(self) -> None:
        self._invitations: dict[bytes, Invitation] = {}
        self._routes: dict[SessionId, str] = {}
        self._authorizations: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    async def issue(self, invitation: Invitation) -> None:
        self._invitations[invitation.token_hash] = invitation

    async def get(self, hash: bytes) -> Invitation | None:
        return self._invitations.get(hash)

    async def claim(
        self, hash: bytes, now: datetime, route: str, expected_user: int | None
    ) -> Invitation:
        async with self._lock:
            invitation = check_claim(self._invitations.get(hash), now, expected_user)
            used = replace(invitation, used_at=now)
            self._invitations[hash] = used
            self._routes[invitation.sid] = route
            return used

    async def route(self, sid: SessionId) -> str | None:
        return self._routes.get(sid)

    async def switch(self, sid: SessionId, expected: str, route: str) -> bool:
        async with self._lock:
            if self._routes.get(sid) != expected:
                return False
            self._routes[sid] = route
            return True

    async def authorize(self, user_id: int, bot_id: int) -> None:
        self._authorizations.add((user_id, bot_id))

    async def authorized(self, user_id: int, bot_id: int) -> bool:
        return (user_id, bot_id) in self._authorizations


class InvitationFlow:
    def __init__(
        self,
        repo: InvitationRepository,
        sender: MessageSender,
        identity: BotIdentity,
        surface: InviteSurface | None,
        clock: Clock,
        rng: Rng,
    ) -> None:
        self._repo = repo
        self._sender = sender
        self._identity = identity
        self._surface = surface
        self._clock = clock
        self._rng = rng

    @property
    def repo(self) -> InvitationRepository:
        return self._repo

    async def issue(
        self, sid: SessionId, counterpart_user_id: int | None, expires_at: datetime
    ) -> str:
        secret = base64.urlsafe_b64encode(self._rng.token_bytes(18)).decode("ascii")
        token = f"{sid.hex}{secret}"
        await self._repo.issue(Invitation(token_hash(token), sid, counterpart_user_id, expires_at))
        return token

    async def link(self, token: str | None = None) -> str:
        username = (await self._identity.bot_username()).removeprefix("@")
        return f"https://t.me/{username}" + ("" if token is None else f"?start={token}")

    async def claim_human(self, token: str, user_id: int) -> SessionId:
        invitation = await self._repo.claim(token_hash(token), self._clock.now(), HUMAN, user_id)
        return invitation.sid

    async def bind_agent(self, token: str, peer: VerifiedPeer) -> SessionId:
        hash = token_hash(token)
        invitation = await self._repo.get(hash)
        if invitation is not None and invitation.counterpart_user_id is None:
            raise InvitationRejected("unbound_invitation")
        if invitation is not None and invitation.counterpart_user_id != peer.user_id:
            raise InvitationRejected("wrong_user")
        if not await self._repo.authorized(peer.user_id, peer.bot_id):
            raise InvitationRejected("agent_not_authorized")
        claimed = await self._repo.claim(
            hash, self._clock.now(), agent_route(peer.bot_id), peer.user_id
        )
        return claimed.sid

    async def expected_user(self, token: str) -> int | None:
        invitation = await self._repo.get(token_hash(token))
        return None if invitation is None else invitation.counterpart_user_id

    async def invite(self, record: SessionRecord) -> str:
        token = await self.issue(record.id, record.counterpart_chat_id, record.deadline)
        link = await self.link(token)
        text = (
            f"{CARD_TEXT} Если у вас есть свой агент, передайте ему эту ссылку, "
            f"а в личке бота отправьте /authorize <ID вашего бота>: {link}"
        )
        try:
            await self._sender.send_message(
                record.counterpart_chat_id, text, business_connection_id=record.connection_id
            )
        except SendRejected:
            logger.warning("invitation_card_failed", sid=str(record.id))
        return token

    async def _article(self, query_id: str) -> dict[str, Any]:
        link = await self.link()
        return {
            "type": "article",
            "id": query_id[:64] or "envoy",
            "title": "Envoy — ИИ-агент",
            "description": "Переговоры от имени владельца; итог подтверждает человек",
            "input_message_content": {"message_text": f"{CARD_TEXT} {link}"},
            "reply_markup": {"inline_keyboard": [[{"text": "Открыть Envoy", "url": link}]]},
        }

    async def on_guest(self, update: GuestMessageUpdate) -> None:
        if self._surface is not None:
            await self._surface.answer_guest(
                update.guest_query_id, await self._article(update.guest_query_id)
            )

    async def on_inline(self, update: InlineQueryUpdate) -> None:
        if self._surface is not None:
            await self._surface.answer_inline(
                update.query_id, [await self._article(update.query_id)]
            )

    async def on_private(self, update: MessageUpdate) -> None:
        parts = (update.text or "").split()
        command = parts[0] if parts else ""
        if command == "/authorize" and len(parts) == 2 and parts[1].isdigit():
            await self._repo.authorize(update.user_id, int(parts[1]))
            reply = f"Агент-бот {parts[1]} может продолжить переговоры от вашего имени."
        elif command == "/start" and len(parts) == 2:
            try:
                await self.claim_human(parts[1], update.user_id)
            except InvitationRejected as error:
                reply = f"Приглашение недействительно ({error.reason})."
            else:
                reply = "Вы продолжаете переговоры с Envoy в исходном чате."
        else:
            reply = f"{CARD_TEXT} {await self.link()}"
        try:
            await self._sender.send_message(update.chat_id, reply)
        except SendRejected:
            logger.warning("invitation_reply_failed", chat=update.chat_id)
