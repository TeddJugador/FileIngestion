"""Vector storage behind one interface: S3 Vectors in AWS, in memory in tests."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

MAX_PER_REQUEST = 500  # S3 Vectors: PutVectors and DeleteVectors each take up to 500
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024  # under the 20 MiB request limit, with headroom


class VectorStoreError(Exception):
    """A vector write or delete failed (retryable from the indexer's point of view)."""


@dataclass(frozen=True)
class VectorRecord:
    key: str
    vector: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class QueryHit:
    key: str
    similarity: float  # cosine similarity: 1 - cosine distance (1 = identical)
    metadata: dict[str, Any]


class VectorStore(Protocol):
    def put(self, records: Sequence[VectorRecord]) -> None: ...

    def delete(self, keys: Sequence[str]) -> None: ...

    def query(
        self, vector: Sequence[float], *, top_k: int, filter: Mapping[str, Any]
    ) -> list[QueryHit]: ...


def audience_filter(school_id: str, audiences: Sequence[str]) -> dict[str, Any]:
    """The authorization boundary, applied by the vector store itself.

    Uses only documented semantics: ``$eq`` on a list field matches if any element
    equals the value (S3 Vectors docs), combined with ``$or`` / ``$and``. ``$in``'s
    behaviour on list fields isn't documented, so it isn't relied on here.
    """
    if not audiences:
        raise ValueError("a caller must have at least one audience")
    return {
        "$and": [
            {"school_id": {"$eq": school_id}},
            {"$or": [{"audience": {"$eq": audience}} for audience in audiences]},
        ]
    }


def _batches(records: Sequence[VectorRecord], size: int) -> Iterator[list[VectorRecord]]:
    batch: list[VectorRecord] = []
    batch_bytes = 0
    for record in records:
        record_bytes = len(json.dumps(record.metadata)) + len(record.vector) * 12 + len(record.key)
        if batch and (len(batch) >= size or batch_bytes + record_bytes > MAX_PAYLOAD_BYTES):
            yield batch
            batch, batch_bytes = [], 0
        batch.append(record)
        batch_bytes += record_bytes
    if batch:
        yield batch


class S3VectorsStore:
    def __init__(
        self,
        client: Any,  # noqa: ANN401 - boto3 "s3vectors" client (no stubs installed)
        *,
        bucket_name: str,
        index_name: str,
        batch_size: int = MAX_PER_REQUEST,
    ) -> None:
        if not 1 <= batch_size <= MAX_PER_REQUEST:
            raise ValueError("batch_size must be 1-500")
        self._client = client
        self._bucket = bucket_name
        self._index = index_name
        self._batch_size = batch_size

    def put(self, records: Sequence[VectorRecord]) -> None:
        for batch in _batches(records, self._batch_size):
            try:
                self._client.put_vectors(
                    vectorBucketName=self._bucket,
                    indexName=self._index,
                    vectors=[
                        {"key": r.key, "data": {"float32": r.vector}, "metadata": r.metadata}
                        for r in batch
                    ],
                )
            except (ClientError, BotoCoreError) as exc:
                raise VectorStoreError(f"put_vectors failed ({type(exc).__name__})") from exc

    def query(
        self, vector: Sequence[float], *, top_k: int, filter: Mapping[str, Any]
    ) -> list[QueryHit]:
        """Cosine index: ``distance`` is 1 - cosine similarity (checked against the
        real service by the integration tests)."""
        try:
            response = self._client.query_vectors(
                vectorBucketName=self._bucket,
                indexName=self._index,
                queryVector={"float32": list(vector)},
                topK=top_k,
                filter=dict(filter),
                returnMetadata=True,
                returnDistance=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise VectorStoreError(f"query_vectors failed ({type(exc).__name__})") from exc
        return [
            QueryHit(
                key=item["key"],
                similarity=1.0 - float(item["distance"]),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in response.get("vectors", [])
        ]

    def delete(self, keys: Sequence[str]) -> None:
        for start in range(0, len(keys), self._batch_size):
            try:
                self._client.delete_vectors(
                    vectorBucketName=self._bucket,
                    indexName=self._index,
                    keys=list(keys[start : start + self._batch_size]),
                )
            except (ClientError, BotoCoreError) as exc:
                raise VectorStoreError(f"delete_vectors failed ({type(exc).__name__})") from exc


class InMemoryVectorStore:
    """Same interface, for unit tests. ``fail_on_put`` makes the Nth put call fail."""

    def __init__(
        self, *, batch_size: int = MAX_PER_REQUEST, fail_on_put: int | None = None
    ) -> None:
        self.vectors: dict[str, VectorRecord] = {}
        self.put_calls: list[int] = []
        self.queries: list[dict[str, Any]] = []
        self._batch_size = batch_size
        self._fail_on_put = fail_on_put

    def put(self, records: Sequence[VectorRecord]) -> None:
        for batch in _batches(records, self._batch_size):
            self.put_calls.append(len(batch))
            if self._fail_on_put is not None and len(self.put_calls) == self._fail_on_put:
                raise VectorStoreError("injected put failure")
            for record in batch:
                self.vectors[record.key] = record

    def delete(self, keys: Sequence[str]) -> None:
        for key in keys:
            self.vectors.pop(key, None)

    def query(
        self, vector: Sequence[float], *, top_k: int, filter: Mapping[str, Any]
    ) -> list[QueryHit]:
        """Exact cosine search with the same filter semantics as S3 Vectors."""
        hits = [
            QueryHit(key=r.key, similarity=_cosine(vector, r.vector), metadata=dict(r.metadata))
            for r in self.vectors.values()
            if matches_filter(r.metadata, filter)
        ]
        self.queries.append(dict(filter))
        return sorted(hits, key=lambda h: h.similarity, reverse=True)[:top_k]

    def keys_for(self, document_id: str, version: str | None = None) -> set[str]:
        prefix = f"{document_id}#{version}#" if version else f"{document_id}#"
        return {k for k in self.vectors if k.startswith(prefix)}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def matches_filter(metadata: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    """The S3 Vectors operators this service uses: $and, $or, $eq (list-aware)."""
    for key, value in condition.items():
        if key == "$and":
            if not all(matches_filter(metadata, c) for c in value):
                return False
        elif key == "$or":
            if not any(matches_filter(metadata, c) for c in value):
                return False
        else:
            expected = value["$eq"] if isinstance(value, dict) else value
            actual = metadata.get(key)
            if isinstance(actual, list):
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
    return True
