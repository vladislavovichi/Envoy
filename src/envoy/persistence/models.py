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
