from typing import NewType
from uuid import UUID, uuid5

OwnerId = NewType("OwnerId", UUID)
AgentId = NewType("AgentId", UUID)
SessionId = NewType("SessionId", UUID)
MandateId = NewType("MandateId", UUID)
OfferHash = NewType("OfferHash", bytes)

OWNER_STREAM_NAMESPACE = UUID("6c1f5c3e-2a8e-4f0b-9b7d-0e2f6d1a9c41")
OWNER_ID_NAMESPACE = UUID("0f8d2c51-7a3b-4e96-8c1d-5b2a4e7f9d03")


def owner_stream(owner: OwnerId) -> SessionId:
    return SessionId(uuid5(OWNER_STREAM_NAMESPACE, str(owner)))


def owner_for_user(user_id: int) -> OwnerId:
    return OwnerId(uuid5(OWNER_ID_NAMESPACE, f"telegram:{user_id}"))
