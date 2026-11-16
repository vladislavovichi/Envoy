from enum import StrEnum


class JournalEntryType(StrEnum):
    session_opened = "session_opened"
    mandate_attested = "mandate_attested"
    offer_sent = "offer_sent"
    offer_received = "offer_received"
    escalated = "escalated"
    decided = "decided"
    closed = "closed"
