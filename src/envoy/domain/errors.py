class ContentErased(LookupError):
    pass


class DecryptionFailed(ContentErased):
    pass


class ChainBroken(ValueError):
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"Journal chain broken at sequence {seq}")


class SignatureInvalid(ValueError):
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"Invalid signature at sequence {seq}")


class SequenceConflict(ValueError):
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"Sequence {seq} already occupied or not contiguous")
