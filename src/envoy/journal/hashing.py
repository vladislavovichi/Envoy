from hashlib import sha256

from envoy.domain.canonical import encode
from envoy.domain.ids import SessionId
from envoy.domain.journal import JournalEntry


def associated_data(session: SessionId, seq: int) -> bytes:
    return encode([session, seq])


def entry_hash(entry: JournalEntry) -> bytes:
    return sha256(
        encode(
            [
                entry.session_id,
                entry.prev_hash,
                entry.seq,
                entry.type.value,
                sha256(entry.payload_ciphertext).digest(),
                entry.author,
                entry.ts,
                entry.mandate_commitment,
            ]
        )
    ).digest()
