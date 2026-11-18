from dataclasses import dataclass, replace

from lucromei.domain import ParsedTransaction, parse_transaction


@dataclass(frozen=True, slots=True)
class ProcessedMessage:
    message_id: str
    whatsapp_number: str
    transaction: ParsedTransaction
    duplicate: bool = False


class InMemoryLedger:
    """MVP adapter used for local development and tests.

    The production adapter will use PostgreSQL with a unique constraint on message_id.
    """

    def __init__(self) -> None:
        self._messages: dict[str, ProcessedMessage] = {}

    def process(self, *, message_id: str, whatsapp_number: str, text: str) -> ProcessedMessage:
        if existing := self._messages.get(message_id):
            return replace(existing, duplicate=True)

        processed = ProcessedMessage(
            message_id=message_id,
            whatsapp_number=whatsapp_number,
            transaction=parse_transaction(text),
        )
        self._messages[message_id] = processed
        return processed


ledger = InMemoryLedger()
