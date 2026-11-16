from collections.abc import Mapping, Sequence

from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import PublicKey
from envoy.domain.errors import ChainBroken, SignatureInvalid
from envoy.domain.ids import AgentId
from envoy.domain.journal import JournalEntry
from envoy.journal.hashing import associated_data, entry_hash
from envoy.journal.ports import KeyVault


class JournalVerifier:
    def verify(self, entries: Sequence[JournalEntry], keys: Mapping[AgentId, PublicKey]) -> None:
        previous = b""
        session = entries[0].session_id if entries else None
        for seq, entry in enumerate(entries):
            if (
                entry.seq != seq
                or entry.prev_hash != previous
                or entry.session_id != session
                or entry_hash(entry) != entry.entry_hash
            ):
                raise ChainBroken(entry.seq)
            public = keys.get(entry.author)
            if public is None or not public.verify(entry.entry_hash, entry.signature):
                raise SignatureInvalid(entry.seq)
            previous = entry.entry_hash

    async def read_payload(
        self, entry: JournalEntry, vault: KeyVault, cipher: ContentCipher
    ) -> bytes:
        key = await vault.entry_key(entry.session_id, entry.seq)
        return cipher.decrypt(
            key, entry.payload_ciphertext, associated_data(entry.session_id, entry.seq)
        )
