import base64
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qsl
from uuid import UUID

import structlog
from aiohttp import web

from envoy.app.owner_signing import (
    ActivateMandate,
    EnrollKey,
    Mutation,
    NoMutation,
    OwnerSigningStore,
    RevokeDelegations,
    SigningRejected,
    StoredChallenge,
    StoreDelegation,
)
from envoy.crypto.keys import PublicKey
from envoy.domain.ids import AgentId, MandateId, OfferHash, OwnerId, SessionId, owner_for_user
from envoy.mandate.commitment import commitment_digest
from envoy.mandate.ports import MandateNotFound, MandateStore, StoredMandate
from envoy.mandate.schema import Mandate
from envoy.mandate.signatures import (
    PURPOSES,
    Choice,
    Purpose,
    enroll_bytes,
    escalation_bytes,
    mandate_bytes,
    revoke_key_bytes,
)
from envoy.ports.clock import Clock
from envoy.ports.rng import Rng
from envoy.protocol.delegation import (
    Delegation,
    delegation_bytes,
    delegation_from_map,
    delegation_map,
)

logger = structlog.get_logger()
CHALLENGE_TTL = timedelta(minutes=10)
INIT_DATA_MAX_AGE = timedelta(hours=1)
DELEGATION_TTL = timedelta(days=30)


class InitDataError(PermissionError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Mini App init data: {reason}")


@dataclass(frozen=True)
class TelegramUser:
    id: int
    auth_date: datetime


def validate_init_data(
    raw: str, bot_token: str, now: datetime, max_age: timedelta = INIT_DATA_MAX_AGE
) -> TelegramUser:
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("malformed") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError("duplicate_field")
        fields[key] = value
    supplied = fields.pop("hash", None)
    if supplied is None:
        raise InitDataError("missing_hash")
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), sha256).digest()
    expected = hmac.new(secret, check.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise InitDataError("bad_hash")
    try:
        auth_date = datetime.fromtimestamp(int(fields["auth_date"]), UTC)
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except KeyError, ValueError, TypeError:
        raise InitDataError("missing_user") from None
    if now - auth_date > max_age or auth_date - now > timedelta(minutes=1):
        raise InitDataError("expired")
    return TelegramUser(user_id, auth_date)


def sign_init_data(fields: Mapping[str, str], bot_token: str) -> str:
    from urllib.parse import urlencode

    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), sha256).digest()
    digest = hmac.new(secret, check.encode(), sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


@dataclass(frozen=True)
class SigningChallenge:
    id: str
    purpose: Purpose
    bytes_to_sign: bytes
    expires_at: datetime


class EscalationGateway(Protocol):
    async def pending_escalation(
        self, sid: SessionId
    ) -> tuple[int, OfferHash, datetime] | None: ...
    async def resolve_signed(self, sid: SessionId, cause: int, approve: bool) -> str: ...


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError, TypeError:
        raise SigningRejected("bad_encoding") from None


class OwnerApi:
    def __init__(
        self,
        store: OwnerSigningStore,
        mandates: MandateStore,
        clock: Clock,
        rng: Rng,
        bot_token: str,
        owner_users: frozenset[int],
        *,
        agent_key: bytes,
        agent_id: AgentId,
        bot_id: int,
        escalations: EscalationGateway | None = None,
        init_max_age: timedelta = INIT_DATA_MAX_AGE,
        delegation_ttl: timedelta = DELEGATION_TTL,
    ) -> None:
        self._store = store
        self._mandates = mandates
        self._clock = clock
        self._rng = rng
        self._token = bot_token
        self._owners = {owner_for_user(user): user for user in owner_users}
        self._agent_key = agent_key
        self._agent_id = agent_id
        self._bot_id = bot_id
        self._escalations = escalations
        self._init_max_age = init_max_age
        self._delegation_ttl = delegation_ttl

    def authenticate(self, init_data: str) -> OwnerId:
        user = validate_init_data(init_data, self._token, self._clock.now(), self._init_max_age)
        owner = owner_for_user(user.id)
        if owner not in self._owners:
            raise InitDataError("not_owner")
        return owner

    @staticmethod
    def schema() -> dict[str, Any]:
        return Mandate.model_json_schema()

    async def active(self, owner: OwnerId) -> dict[str, Any]:
        stored = await self._mandates.active(owner)
        key = await self._store.owner_key(owner)
        delegation = await self._store.current(owner)
        return {
            "enrolled": key is not None,
            "mandate": (
                None
                if stored is None
                else {
                    "id": str(stored.id),
                    "version": stored.version,
                    "attestation": stored.attestation,
                    "body": stored.mandate.model_dump(mode="json"),
                }
            ),
            "delegation": (
                None
                if delegation is None
                else {
                    "mandate_id": str(delegation.mandate_id),
                    "version": delegation.version,
                    "expires_at": delegation.expires_at.isoformat(),
                }
            ),
        }

    def _nonce(self) -> bytes:
        return self._rng.token_bytes(16)

    def _id(self) -> str:
        return b64(self._rng.token_bytes(18))

    async def create_challenge(
        self, owner: OwnerId, purpose: str, params: Mapping[str, Any]
    ) -> SigningChallenge:
        if purpose not in PURPOSES:
            raise SigningRejected("unknown_purpose")
        now = self._clock.now()
        challenge_id = self._id()
        nonce = self._nonce()
        payload, context = await self._prepare(owner, purpose, params, challenge_id, nonce)
        challenge = StoredChallenge(
            id=challenge_id,
            owner=owner,
            purpose=purpose,
            nonce=nonce,
            bytes_to_sign=payload,
            expires_at=now + CHALLENGE_TTL,
            context=context,
        )
        await self._store.put_challenge(challenge)
        return SigningChallenge(challenge_id, challenge.purpose, payload, challenge.expires_at)

    async def _draft(self, owner: OwnerId, params: Mapping[str, Any]) -> StoredMandate:
        if params.get("copy_active"):
            active = await self._mandates.active(owner)
            if active is None:
                raise SigningRejected("no_active_mandate")
            return await self._mandates.save_draft(owner, active.mandate, self._rng.token_bytes(32))
        if "body" in params:
            try:
                mandate = Mandate.model_validate(params["body"])
            except ValueError:
                raise SigningRejected("invalid_mandate") from None
            return await self._mandates.save_draft(owner, mandate, self._rng.token_bytes(32))
        try:
            draft = await self._mandates.get(MandateId(UUID(str(params["mandate_id"]))))
        except KeyError, ValueError, MandateNotFound:
            raise SigningRejected("unknown_mandate") from None
        if draft.owner != owner or draft.status != "draft":
            raise SigningRejected("not_a_draft")
        return draft

    async def _prepare(
        self,
        owner: OwnerId,
        purpose: Purpose,
        params: Mapping[str, Any],
        challenge: str,
        nonce: bytes,
    ) -> tuple[bytes, dict[str, Any]]:
        match purpose:
            case "enroll":
                key = unb64(str(params.get("public_key", "")))
                if len(key) != 32:
                    raise SigningRejected("bad_key")
                existing = await self._store.owner_key(owner)
                if existing is not None and existing != key:
                    raise SigningRejected("already_enrolled")
                return enroll_bytes(owner, key, challenge, nonce), {"public_key": key}
            case "mandate":
                draft = await self._draft(owner, params)
                active = await self._mandates.active(owner)
                expected = None if active is None else active.version
                payload = mandate_bytes(
                    owner, draft.id, draft.version, draft.mandate, challenge, nonce
                )
                return payload, {"mandate_id": draft.id, "expected_version": expected}
            case "delegate":
                delegation = await self._delegation(owner)
                return delegation_bytes(delegation), {"delegation": delegation_map(delegation)}
            case "escalation":
                return await self._escalation(owner, params, challenge, nonce)
            case "revoke_key":
                current = await self._store.current(owner)
                if current is None:
                    raise SigningRejected("no_delegation")
                revision = int(self._clock.now().timestamp())
                payload = revoke_key_bytes(owner, current.agent_key, revision, challenge, nonce)
                return payload, {"agent_key": current.agent_key}

    async def _delegation(self, owner: OwnerId) -> Delegation:
        key = await self._store.owner_key(owner)
        active = await self._mandates.active(owner)
        if key is None:
            raise SigningRejected("not_enrolled")
        if active is None or active.attestation != "owner":
            raise SigningRejected("mandate_not_signed")
        return Delegation(
            owner_id=owner,
            owner_key=key,
            agent_id=self._agent_id,
            bot_id=self._bot_id,
            agent_key=self._agent_key,
            mandate_id=active.id,
            version=active.version,
            commitment=commitment_digest(active.mandate, active.salt),
            expires_at=self._clock.now() + self._delegation_ttl,
        )

    async def _escalation(
        self, owner: OwnerId, params: Mapping[str, Any], challenge: str, nonce: bytes
    ) -> tuple[bytes, dict[str, Any]]:
        if self._escalations is None:
            raise SigningRejected("no_escalations")
        try:
            sid = SessionId(UUID(str(params["sid"])))
            choice = cast(Choice, str(params["choice"]))
        except KeyError, ValueError:
            raise SigningRejected("bad_escalation") from None
        if choice not in ("approve", "decline"):
            raise SigningRejected("bad_escalation")
        pending = await self._escalations.pending_escalation(sid)
        if pending is None:
            raise SigningRejected("no_pending_escalation")
        cause, offer_hash, expires = pending
        payload = escalation_bytes(owner, sid, cause, offer_hash, choice, expires, challenge, nonce)
        context = {"sid": sid, "cause": cause, "offer_hash": offer_hash, "choice": choice}
        return payload, context

    async def confirm(
        self, owner: OwnerId, challenge_id: str, signature: bytes
    ) -> Mapping[str, Any]:
        challenge = await self._store.get_challenge(challenge_id)
        if challenge is None or challenge.owner != owner:
            raise SigningRejected("unknown_challenge")
        if challenge.signature is not None and challenge.signature == signature:
            return challenge.result or {}
        key = (
            challenge.context["public_key"]
            if challenge.purpose == "enroll"
            else await self._store.owner_key(owner)
        )
        if key is None:
            raise SigningRejected("not_enrolled")
        if not PublicKey.from_bytes(key).verify(challenge.bytes_to_sign, signature):
            raise SigningRejected("bad_signature")
        mutation, result = await self._mutation(owner, challenge, signature)
        redeemed = await self._store.redeem(
            challenge_id, signature, mutation, result, self._clock.now()
        )
        if challenge.purpose == "escalation" and self._escalations is not None:
            context = challenge.context
            answer = await self._escalations.resolve_signed(
                SessionId(context["sid"]), int(context["cause"]), context["choice"] == "approve"
            )
            return {**(redeemed.result or {}), "answer": answer}
        return redeemed.result or {}

    async def _mutation(
        self, owner: OwnerId, challenge: StoredChallenge, signature: bytes
    ) -> tuple[Mutation, dict[str, Any]]:
        context = challenge.context
        match challenge.purpose:
            case "enroll":
                return EnrollKey(owner, context["public_key"]), {"enrolled": True}
            case "mandate":
                mandate_id = MandateId(context["mandate_id"])
                return (
                    ActivateMandate(owner, mandate_id, context["expected_version"], signature),
                    {"mandate_id": str(mandate_id)},
                )
            case "delegate":
                delegation = delegation_from_map({**context["delegation"], "signature": signature})
                return StoreDelegation(delegation), {"delegated": delegation.version}
            case "escalation":
                pending = None
                if self._escalations is not None:
                    pending = await self._escalations.pending_escalation(SessionId(context["sid"]))
                if pending is None or pending[:2] != (context["cause"], context["offer_hash"]):
                    raise SigningRejected("stale_escalation")
                return NoMutation(), {"choice": context["choice"]}
            case "revoke_key":
                return RevokeDelegations(owner, context["agent_key"]), {"revoked": True}

    def app(self, static: Path | None = None) -> web.Application:
        app = web.Application()
        if static is not None and (static / "index.html").is_file():
            index = static / "index.html"

            async def page(request: web.Request) -> web.FileResponse:
                return web.FileResponse(index)

            app.router.add_get("/app/", page)
            app.router.add_static("/app/assets/", static / "assets")
        app.router.add_get("/miniapp/mandates/schema", self._http_schema)
        app.router.add_get("/miniapp/mandates/active", self._http_active)
        app.router.add_post("/miniapp/challenges", self._http_challenge)
        app.router.add_post("/miniapp/challenges/{id}/signature", self._http_signature)
        return app

    def _owner(self, request: web.Request) -> OwnerId:
        header = request.headers.get("Authorization", "")
        scheme, _, data = header.partition(" ")
        if scheme != "tma" or not data:
            raise web.HTTPUnauthorized()
        try:
            return self.authenticate(data)
        except InitDataError as error:
            logger.info("miniapp_auth_rejected", reason=error.reason)
            raise web.HTTPUnauthorized() from None

    async def _http_schema(self, request: web.Request) -> web.Response:
        self._owner(request)
        return web.json_response(self.schema())

    async def _http_active(self, request: web.Request) -> web.Response:
        return web.json_response(await self.active(self._owner(request)))

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        return body

    async def _http_challenge(self, request: web.Request) -> web.Response:
        owner = self._owner(request)
        body = await self._json(request)
        try:
            challenge = await self.create_challenge(
                owner, str(body.get("purpose", "")), body.get("params") or {}
            )
        except SigningRejected as error:
            return web.json_response({"error": error.reason}, status=409)
        return web.json_response(
            {
                "id": challenge.id,
                "purpose": challenge.purpose,
                "bytes_to_sign": b64(challenge.bytes_to_sign),
                "expires_at": challenge.expires_at.isoformat(),
            }
        )

    async def _http_signature(self, request: web.Request) -> web.Response:
        owner = self._owner(request)
        body = await self._json(request)
        try:
            result = await self.confirm(
                owner, request.match_info["id"], unb64(str(body.get("signature", "")))
            )
        except SigningRejected as error:
            return web.json_response({"error": error.reason}, status=409)
        return web.json_response(_jsonable(result))


def _jsonable(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: str(item) if isinstance(item, UUID | bytes) else item for key, item in value.items()
    }
