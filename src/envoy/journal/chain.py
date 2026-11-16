from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.errors import SequenceConflict
from envoy.domain.events import JournalEntryType
from envoy.domain.ids import AgentId, SessionId
from envoy.domain.journal import JournalEntry
from envoy.journal.hashing import associated_data, entry_hash
from envoy.journal.ports import JournalCommitter, JournalReader, KeyVault
from envoy.ports.clock import Clock


class JournalAuthor:
    def __init__(
        self,
        session: SessionId,
        agent: AgentId,
        signing_key: SigningKey,
        writer: JournalCommitter,
        reader: JournalReader,
        vault: KeyVault,
        cipher: ContentCipher,
        clock: Clock,
    ) -> None:
        self._session = session
        self._agent = agent
        self._signing_key = signing_key
        self._writer = writer
        self._reader = reader
        self._vault = vault
        self._cipher = cipher
        self._clock = clock

    async def record(
        self, type: JournalEntryType, payload: bytes, mandate_commitment: bytes
    ) -> JournalEntry:
        attempt = 0
        while True:
            head = await self._reader.head(self._session)
            seq = 0 if head is None else head.seq + 1
            key = self._cipher.generate_key()
            entry = JournalEntry(
                session_id=self._session,
                seq=seq,
                prev_hash=b"" if head is None else head.entry_hash,
                type=type,
                payload_ciphertext=self._cipher.encrypt(
                    key, payload, associated_data(self._session, seq)
                ),
                author=self._agent,
                ts=self._clock.now(),
                mandate_commitment=mandate_commitment,
                entry_hash=b"",
                signature=b"",
            )
            digest = entry_hash(entry)
            entry = entry.model_copy(
                update={"entry_hash": digest, "signature": self._signing_key.sign(digest)}
            )
            try:
                await self._writer.commit(entry, key)
            except SequenceConflict:
                if attempt == 3:
                    raise
                attempt += 1
                continue
            return entry
