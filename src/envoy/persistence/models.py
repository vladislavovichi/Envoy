from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, LargeBinary, String, Uuid
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
