import pytest

from envoy.journal.contract import JournalStoreContract
from envoy.journal.memory import InMemoryJournalStore


class TestMemoryJournalStore(JournalStoreContract):
    @pytest.fixture
    def store(self):
        return InMemoryJournalStore()
