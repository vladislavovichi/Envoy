from typing import NewType
from uuid import UUID

OwnerId = NewType("OwnerId", UUID)
AgentId = NewType("AgentId", UUID)
SessionId = NewType("SessionId", UUID)
MandateId = NewType("MandateId", UUID)
OfferHash = NewType("OfferHash", bytes)
