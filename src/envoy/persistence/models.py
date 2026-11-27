from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JournalRow(Base):
    __tablename__ = "journal_entries"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary)
    type: Mapped[str] = mapped_column(String(40))
    payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    author: Mapped[UUID] = mapped_column(Uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mandate_commitment: Mapped[bytes] = mapped_column(LargeBinary)
    entry_hash: Mapped[bytes] = mapped_column(LargeBinary)
    signature: Mapped[bytes] = mapped_column(LargeBinary)


class EntryKeyRow(Base):
    __tablename__ = "entry_keys"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)


class MandateRow(Base):
    __tablename__ = "mandates"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    version: Mapped[int] = mapped_column(Integer)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16))
    signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    attestation: Mapped[str] = mapped_column(String(16), server_default="platform")
    __table_args__ = (UniqueConstraint("owner_id", "version"),)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    mandate_id: Mapped[UUID] = mapped_column(Uuid)
    connection_id: Mapped[str] = mapped_column(String(128))
    counterpart_chat_id: Mapped[int] = mapped_column(BigInteger)
    owner_chat_id: Mapped[int] = mapped_column(BigInteger)
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dashboard_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dashboard_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dashboard_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dashboard_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (Index("ix_sessions_chat", "connection_id", "counterpart_chat_id"),)


class BusinessConnectionRow(Base):
    __tablename__ = "business_connections"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    user_chat_id: Mapped[int] = mapped_column(BigInteger)
    rights: Mapped[dict[str, bool]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean)


class BusinessChatRow(Base):
    __tablename__ = "business_chats"
    connection_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_incoming_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedUpdateRow(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(String(16))


class ScheduledJobRow(Base):
    __tablename__ = "scheduled_jobs"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    session_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))


class LlmUsageRow(Base):
    __tablename__ = "llm_usage"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens: Mapped[int] = mapped_column(BigInteger)


class EscalationWidgetRow(Base):
    __tablename__ = "escalation_widgets"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cause: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_escalation_widgets_message"),
    )


class SummaryWidgetRow(Base):
    __tablename__ = "summary_widgets"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list[Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_summary_widgets_message"),)


class OwnerSummaryRow(Base):
    __tablename__ = "owner_summaries"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class OwnerSummaryTopicRow(Base):
    __tablename__ = "owner_summary_topics"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger)


class PeerPinRow(Base):
    __tablename__ = "peer_pins"
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid)
    owner_key: Mapped[bytes] = mapped_column(LargeBinary)
    agent_id: Mapped[UUID] = mapped_column(Uuid)
    agent_key: Mapped[bytes] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer)
    revoked: Mapped[bool] = mapped_column(Boolean)


class PeerKeyHistoryRow(Base):
    __tablename__ = "peer_key_history"
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    agent_key: Mapped[bytes] = mapped_column(LargeBinary)


class ProtocolSessionRow(Base):
    __tablename__ = "protocol_sessions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    peer_bot_id: Mapped[int] = mapped_column(BigInteger)
    owner_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    closed: Mapped[bool] = mapped_column(Boolean)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    __table_args__ = (Index("ix_protocol_sessions_pair", "bot_id", "peer_bot_id", "closed"),)


class ProtocolUpdateRow(Base):
    __tablename__ = "protocol_updates"
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)


class ProtocolNonceRow(Base):
    __tablename__ = "protocol_nonces"
    sender: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProtocolEvidenceRow(Base):
    __tablename__ = "protocol_evidence"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    text_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)


class OwnerKeyRow(Base):
    __tablename__ = "owner_keys"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)


class SigningChallengeRow(Base):
    __tablename__ = "signing_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    bytes_to_sign: Mapped[bytes] = mapped_column(LargeBinary)
    context: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    result: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class OwnerDelegationRow(Base):
    __tablename__ = "owner_delegations"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    agent_key: Mapped[bytes] = mapped_column(LargeBinary)
    body: Mapped[bytes] = mapped_column(LargeBinary)
    revoked: Mapped[bool] = mapped_column(Boolean)


class InvitationRow(Base):
    __tablename__ = "invitations"
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    counterpart_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionRouteRow(Base):
    __tablename__ = "session_routes"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    route: Mapped[str] = mapped_column(String(64))


class AgentAuthorizationRow(Base):
    __tablename__ = "agent_authorizations"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)


class MandateCardRow(Base):
    __tablename__ = "mandate_widgets"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid)
    user_id: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer)
    draft_id: Mapped[UUID] = mapped_column(Uuid)
    slots: Mapped[list[Any]] = mapped_column(JSON)
    selected: Mapped[list[Any]] = mapped_column(JSON)
    issues: Mapped[list[Any]] = mapped_column(JSON)


class ManagedBotRow(Base):
    __tablename__ = "managed_bots"
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_user: Mapped[int] = mapped_column(BigInteger)
    secret: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    trusted: Mapped[list[Any]] = mapped_column(JSON)
