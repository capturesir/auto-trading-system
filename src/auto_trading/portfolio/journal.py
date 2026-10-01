"""Trade journal.

Each fill should be traceable to the signal and the approved strategy
version that motivated it. Persistence goes through the repository.
Not implemented yet.
"""


class TradeJournal:
    """Placeholder append-only trade journal."""

    def record(self, fill: object) -> None:
        raise NotImplementedError("trade journal is not implemented yet")
