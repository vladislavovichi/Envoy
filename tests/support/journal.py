from uuid import UUID

from envoy.app.journal import SessionJournal
from envoy.crypto.aead import ContentCipher
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, SessionId
from envoy.journal.chain import JournalAuthor
from envoy.journal.memory import InMemoryJournalStore, InMemoryKeyVault
from envoy.ports.clock import Clock
from envoy.ports.rng import SeededRng

AGENT = AgentId(UUID(int=77))


def build_journal(
    clock: Clock, key: SigningKey | None = None
) -> tuple[SessionJournal, InMemoryJournalStore, SigningKey]:
    signing = key or SigningKey.generate()
    vault = InMemoryKeyVault()
    store = InMemoryJournalStore(vault)
    cipher = ContentCipher(SeededRng(3))

    def author(session: SessionId) -> JournalAuthor:
        return JournalAuthor(session, AGENT, signing, store, store, vault, cipher, clock)

    return SessionJournal(author, store, vault, cipher), store, signing
