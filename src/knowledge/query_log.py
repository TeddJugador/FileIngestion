"""Who asked which question: enough to check feedback ownership, nothing more.

Items hold the query ID, the asker's user ID and an expiry (7 days). Never the question
or the answer: people type names and personal details into questions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

TTL = timedelta(days=7)


class QueryLog:
    def __init__(self, client: Any, table_name: str) -> None:  # noqa: ANN401 - boto3 client
        self._client = client
        self._table = table_name

    def record(self, query_id: str, asker_id: str, now: datetime) -> None:
        self._client.put_item(
            TableName=self._table,
            Item={
                "queryId": {"S": query_id},
                "askerId": {"S": asker_id},
                "expires_at": {"N": str(int((now + TTL).timestamp()))},
            },
        )

    def asker_of(self, query_id: str) -> str | None:
        item = self._client.get_item(
            TableName=self._table, Key={"queryId": {"S": query_id}}, ConsistentRead=True
        ).get("Item")
        return item["askerId"]["S"] if item else None
