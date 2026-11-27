from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, MandateId, OwnerId, SessionId
from envoy.protocol.bodies import version_map
from envoy.protocol.delegation import (
    Delegation,
    delegation_map,
    sign_delegation,
)
from envoy.protocol.envelope import (
    WIRE_VERSION,
    Envelope,
    Version,
    decode_message,
    encode_message,
)
from tests.support.sessions import T0

SID = SessionId(UUID("01936d1c-7b5a-7c3e-8f00-000000000001"))


@dataclass
class Party:
    bot_id: int
    seed: int
    owner: SigningKey = field(init=False)
    agent: SigningKey = field(init=False)

    def __post_init__(self) -> None:
        self.owner = SigningKey.from_bytes(bytes([self.seed]) * 32)
        self.agent = SigningKey.from_bytes(bytes([self.seed + 100]) * 32)

    @property
    def owner_id(self) -> OwnerId:
        return OwnerId(UUID(int=self.seed))

    @property
    def agent_id(self) -> AgentId:
        return AgentId(UUID(int=1000 + self.seed))

    def delegation(
        self,
        *,
        expires_at: datetime | None = None,
        agent: SigningKey | None = None,
        bot_id: int | None = None,
        owner: SigningKey | None = None,
    ) -> Delegation:
        signer = owner or self.owner
        return sign_delegation(
            Delegation(
                owner_id=self.owner_id,
                owner_key=signer.public().to_bytes(),
                agent_id=self.agent_id,
                bot_id=self.bot_id if bot_id is None else bot_id,
                agent_key=(agent or self.agent).public().to_bytes(),
                mandate_id=MandateId(UUID(int=77)),
                version=1,
                commitment=bytes(32),
                expires_at=expires_at or T0 + timedelta(days=30),
            ),
            signer,
        )

    def hello(
        self,
        *,
        delegation: Delegation | None = None,
        signer: SigningKey | None = None,
        versions: tuple[Version, Version] = (WIRE_VERSION, WIRE_VERSION),
        crit: tuple[str, ...] = (),
        major: int | None = None,
        token: str | None = None,
    ):
        delegation = delegation or self.delegation()
        version = WIRE_VERSION if major is None else Version(major, 0)
        text = encode_message(
            Envelope(
                v=version,
                sid=SID,
                seq=1,
                ack=0,
                type="hello",
                body={
                    "agent_id": delegation.agent_id.bytes,
                    "owner_id": delegation.owner_id.bytes,
                    "min_version": version_map(versions[0]),
                    "max_version": version_map(versions[1]),
                    "token": token,
                },
                ts=int(T0.timestamp()),
                exp=int(T0.timestamp()) + 60,
                nonce=bytes(16),
                chain=delegation_map(delegation),
                crit=crit,
            ),
            signer or self.agent,
        )
        return decode_message(text)


ALICE = Party(bot_id=501, seed=1)
BOB = Party(bot_id=502, seed=2)
