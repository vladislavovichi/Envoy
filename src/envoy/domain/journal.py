from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from envoy.domain.events import JournalEntryType
from envoy.domain.ids import AgentId, SessionId


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: SessionId
    seq: int = Field(ge=0)
    prev_hash: bytes
    type: JournalEntryType
    payload_ciphertext: bytes
    author: AgentId
    ts: AwareDatetime
    mandate_commitment: bytes
    entry_hash: bytes
    signature: bytes
