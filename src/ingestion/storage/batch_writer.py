"""Buffered ``BatchWriteItem`` with retries for ``UnprocessedItems``.

Notes:
- ``BatchWriteItem`` reduces the number of requests, not the write capacity used: each
  item still costs its own WCUs.
- It has no condition expressions, so per-item idempotency isn't possible here. That's
  fine: idempotency is handled at file level (the import ID), and rewriting an item
  with the same PK and SK is naturally idempotent.
- A batch may not contain two items with the same key; the caller removes duplicate
  natural keys within a file before they reach this writer.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

BATCH_SIZE = 25  # DynamoDB maximum per BatchWriteItem request


class UnprocessedItemsError(Exception):
    """Items were still unprocessed after all retries; the SQS message must be retried."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} item(s) still unprocessed after retries")
        self.count = count


class BatchWriter:
    def __init__(
        self,
        client: DynamoDBClient,
        table_name: str,
        *,
        max_attempts: int = 5,
        base_delay: float = 0.05,
        max_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._table = table_name
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._jitter = jitter
        self._pending: list[Mapping[str, Any]] = []
        self.items_written = 0

    def put(self, item: Mapping[str, Any]) -> None:
        """Queue one item, already in DynamoDB attribute-value format."""
        self._pending.append(item)
        if len(self._pending) >= BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        requests: list[Any] = [{"PutRequest": {"Item": item}} for item in batch]

        for attempt in range(1, self._max_attempts + 1):
            response = self._client.batch_write_item(RequestItems={self._table: requests})
            requests = list(response.get("UnprocessedItems", {}).get(self._table, []))
            if not requests:
                self.items_written += len(batch)
                return
            if attempt < self._max_attempts:
                # Exponential backoff with full jitter.
                ceiling = min(self._max_delay, self._base_delay * 2 ** (attempt - 1))
                self._sleep(ceiling * self._jitter())
        raise UnprocessedItemsError(len(requests))
