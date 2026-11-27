import asyncio
import base64
from datetime import timedelta
from uuid import UUID, uuid7

import pytest

from envoy.app.miniapp import OwnerApi, sign_init_data
from envoy.app.owner_signing import InMemoryOwnerSigningStore, SigningRejected
from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, OfferHash, SessionId, owner_for_user
from envoy.mandate.ports import InMemoryMandateStore
from envoy.persistence.owner_signing import PgOwnerSigningStore
from envoy.persistence.state_stores import PgMandateStore
from envoy.ports.clock import FixedClock
from envoy.ports.ids import SequentialIdGenerator, UuidV7Generator
from envoy.ports.rng import SeededRng, SystemRng
from envoy.protocol.delegation import delegation_bytes, verify_delegation
from tests.support.mandates import meeting_data, meeting_mandate
from tests.support.sessions import T0

TOKEN = "123456:TEST"
USER, STRANGER = 1, 2
OWNER = owner_for_user(USER)
OWNER_KEY = SigningKey.from_bytes(bytes([5]) * 32)
AGENT = SigningKey.from_bytes(bytes([6]) * 32)


class Gateway:
    def __init__(self) -> None:
        self.pending: dict[SessionId, tuple[int, OfferHash, object]] = {}
        self.resolved: list[tuple[SessionId, int, bool]] = []

    async def pending_escalation(self, sid):
        return self.pending.get(sid)

    async def resolve_signed(self, sid, cause, approve):
        self.resolved.append((sid, cause, approve))
        del self.pending[sid]
        return "Одобрено" if approve else "Отклонено"


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    return request.param


@pytest.fixture
def parts(backend, request):
    clock = FixedClock(T0)
    if backend == "memory":
        mandates = InMemoryMandateStore(SequentialIdGenerator())
        store = InMemoryOwnerSigningStore(mandates)
    else:
        factory = request.getfixturevalue("pg_session_factory")
        cipher = ContentCipher(SystemRng())
        mandates = PgMandateStore(factory, UuidV7Generator(), cipher, cipher.generate_key())
        store = PgOwnerSigningStore(factory, mandates)
    gateway = Gateway()
    api = OwnerApi(
        store,
        mandates,
        clock,
        SeededRng(4),
        TOKEN,
        frozenset({USER, STRANGER}),
        agent_key=AGENT.public().to_bytes(),
        agent_id=AgentId(UUID(int=9)),
        bot_id=501,
        escalations=gateway,
    )
    return api, store, mandates, clock, gateway


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def sign(api, purpose, params, key=OWNER_KEY, owner=OWNER):
    challenge = await api.create_challenge(owner, purpose, params)
    return challenge, key.sign(challenge.bytes_to_sign)


async def enroll(api):
    params = {"public_key": b64(OWNER_KEY.public().to_bytes())}
    challenge, signature = await sign(api, "enroll", params)
    return await api.confirm(OWNER, challenge.id, signature)


async def activate(api, **overrides):
    challenge, signature = await sign(api, "mandate", {"body": meeting_data(**overrides)})
    return await api.confirm(OWNER, challenge.id, signature)


async def test_enroll_pins_first_key_and_rejects_another(parts) -> None:
    api, store, *_ = parts
    assert await enroll(api) == {"enrolled": True}
    assert await store.owner_key(OWNER) == OWNER_KEY.public().to_bytes()
    other = SigningKey.generate()
    with pytest.raises(SigningRejected, match="already_enrolled"):
        await sign(api, "enroll", {"public_key": b64(other.public().to_bytes())}, other)


async def test_concurrent_enrolls_with_different_keys_pin_one(parts) -> None:
    api, store, *_ = parts
    other = SigningKey.generate()
    first = await sign(api, "enroll", {"public_key": b64(OWNER_KEY.public().to_bytes())})
    second = await sign(api, "enroll", {"public_key": b64(other.public().to_bytes())}, other)
    results = await asyncio.gather(
        api.confirm(OWNER, first[0].id, first[1]),
        api.confirm(OWNER, second[0].id, second[1]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SigningRejected) for result in results) == 1
    assert await store.owner_key(OWNER) in {
        OWNER_KEY.public().to_bytes(),
        other.public().to_bytes(),
    }


async def test_signed_activation_replay_and_tampering(parts) -> None:
    api, store, mandates, *_ = parts
    await enroll(api)
    challenge, signature = await sign(api, "mandate", {"body": meeting_data()})
    with pytest.raises(SigningRejected, match="bad_signature"):
        await api.confirm(OWNER, challenge.id, OWNER_KEY.sign(challenge.bytes_to_sign + b"x"))
    with pytest.raises(SigningRejected, match="bad_signature"):
        await api.confirm(OWNER, challenge.id, SigningKey.generate().sign(challenge.bytes_to_sign))
    result = await api.confirm(OWNER, challenge.id, signature)
    assert await api.confirm(OWNER, challenge.id, signature) == result
    active = await mandates.active(OWNER)
    assert (active.attestation, str(active.id)) == ("owner", result["mandate_id"])
    other, _ = await sign(api, "mandate", {"body": meeting_data()})
    with pytest.raises(SigningRejected, match="bad_signature"):
        await api.confirm(OWNER, other.id, signature)
    with pytest.raises(SigningRejected, match="unknown_challenge"):
        await api.confirm(owner_for_user(STRANGER), challenge.id, signature)


async def test_stale_version_and_concurrent_confirmations_activate_once(parts) -> None:
    api, store, mandates, *_ = parts
    await enroll(api)
    first = await sign(api, "mandate", {"body": meeting_data()})
    second = await sign(api, "mandate", {"body": meeting_data()})
    results = await asyncio.gather(
        api.confirm(OWNER, first[0].id, first[1]),
        api.confirm(OWNER, second[0].id, second[1]),
        return_exceptions=True,
    )
    rejected = [result for result in results if isinstance(result, SigningRejected)]
    assert len(rejected) == 1
    assert "stale_version" in str(rejected[0])
    active = await mandates.active(OWNER)
    accepted = [result for result in results if not isinstance(result, Exception)]
    assert [result["mandate_id"] for result in accepted] == [str(active.id)]


async def test_expired_challenge_is_rejected(parts) -> None:
    api, _, _, clock, _ = parts
    await enroll(api)
    challenge, signature = await sign(api, "mandate", {"body": meeting_data()})
    clock.advance(timedelta(minutes=10))
    with pytest.raises(SigningRejected, match="challenge_expired"):
        await api.confirm(OWNER, challenge.id, signature)


async def test_reissue_keeps_platform_version_for_old_sessions_and_delegates_signed(parts) -> None:
    api, store, mandates, clock, _ = parts
    draft = await mandates.save_draft(OWNER, meeting_mandate(), b"s" * 32)
    platform = await mandates.activate(draft.id, b"p" * 64)
    assert platform.attestation == "platform"
    await enroll(api)
    with pytest.raises(SigningRejected, match="mandate_not_signed"):
        await api.create_challenge(OWNER, "delegate", {})
    challenge, signature = await sign(api, "mandate", {"copy_active": True})
    await api.confirm(OWNER, challenge.id, signature)
    reissued = await mandates.active(OWNER)
    assert (reissued.version, reissued.attestation) == (2, "owner")
    old = await mandates.get(platform.id)
    assert (old.status, old.attestation, old.mandate) == (
        "superseded",
        "platform",
        platform.mandate,
    )
    challenge, signature = await sign(api, "delegate", {})
    await api.confirm(OWNER, challenge.id, signature)
    delegation = await store.current(OWNER)
    assert challenge.bytes_to_sign == delegation_bytes(delegation)
    assert (delegation.mandate_id, delegation.version) == (reissued.id, 2)
    verify_delegation(delegation, now=clock.now(), bot_id=501)
    challenge, signature = await sign(api, "revoke_key", {})
    await api.confirm(OWNER, challenge.id, signature)
    assert await store.current(OWNER) is None


async def test_escalation_signature_binds_session_and_offer(parts) -> None:
    api, _, _, _, gateway = parts
    await enroll(api)
    sid = SessionId(uuid7())
    gateway.pending[sid] = (5, OfferHash(bytes(32)), T0 + timedelta(hours=1))
    with pytest.raises(SigningRejected, match="no_pending_escalation"):
        await api.create_challenge(OWNER, "escalation", {"sid": str(uuid7()), "choice": "approve"})
    challenge, signature = await sign(api, "escalation", {"sid": str(sid), "choice": "approve"})
    gateway.pending[sid] = (5, OfferHash(bytes([1]) * 32), T0 + timedelta(hours=1))
    with pytest.raises(SigningRejected, match="stale_escalation"):
        await api.confirm(OWNER, challenge.id, signature)
    assert gateway.resolved == []
    challenge, signature = await sign(api, "escalation", {"sid": str(sid), "choice": "decline"})
    result = await api.confirm(OWNER, challenge.id, signature)
    assert result["answer"] == "Отклонено"
    assert gateway.resolved == [(sid, 5, False)]


async def test_http_api_requires_init_data_and_round_trips(parts, aiohttp_server) -> None:
    import aiohttp

    api, *_ = parts
    server = await aiohttp_server(api.app())
    init = sign_init_data({"auth_date": str(int(T0.timestamp())), "user": '{"id": 1}'}, TOKEN)
    headers = {"Authorization": f"tma {init}"}
    async with aiohttp.ClientSession() as client:
        async with client.get(server + "/miniapp/mandates/active") as response:
            assert response.status == 401
        async with client.get(server + "/miniapp/mandates/schema", headers=headers) as response:
            assert "properties" in await response.json()
        async with client.post(
            server + "/miniapp/challenges",
            headers=headers,
            json={
                "purpose": "enroll",
                "params": {"public_key": b64(OWNER_KEY.public().to_bytes())},
            },
        ) as response:
            challenge = await response.json()
        raw = base64.urlsafe_b64decode(challenge["bytes_to_sign"] + "==")
        async with client.post(
            server + f"/miniapp/challenges/{challenge['id']}/signature",
            headers=headers,
            json={"signature": b64(OWNER_KEY.sign(raw))},
        ) as response:
            assert await response.json() == {"enrolled": True}
        async with client.get(server + "/miniapp/mandates/active", headers=headers) as response:
            assert (await response.json())["enrolled"] is True
        async with client.post(
            server + "/miniapp/challenges", headers=headers, json={"purpose": "teleport"}
        ) as response:
            assert response.status == 409


async def test_built_mini_app_is_served_next_to_the_api(parts, aiohttp_server, tmp_path) -> None:
    import aiohttp

    api, *_ = parts
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Envoy</title>", "utf-8")
    (tmp_path / "assets" / "index.js").write_text("console.log(1)", "utf-8")
    server = await aiohttp_server(api.app(tmp_path))
    async with aiohttp.ClientSession() as client:
        async with client.get(server + "/app/") as response:
            assert "Envoy" in await response.text()
        async with client.get(server + "/app/assets/index.js") as response:
            assert response.status == 200
    bare = await aiohttp_server(api.app(tmp_path / "missing"))
    async with aiohttp.ClientSession() as client:
        async with client.get(bare + "/app/") as response:
            assert response.status == 404
