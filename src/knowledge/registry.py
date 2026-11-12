"""DocumentRegistry: one META item per document, one VER item per version.

| Item     | PK                                    | SK             |
|----------|---------------------------------------|----------------|
| Document | SCHOOL#<school_id>#DOC#<document_id>  | META           |
| Version  | same                                  | VER#<version>  |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class Status(StrEnum):
    INDEXING = "INDEXING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    RETIRED = "RETIRED"
    SUPERSEDED = "SUPERSEDED"  # versions only


class ActivationConflictError(Exception):
    """META.active_version changed since we read it: another version finished first."""


@dataclass(frozen=True)
class DocumentRecord:
    school_id: str
    document_id: str
    status: str
    active_version: str | None
    title: str
    audience: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class VersionRecord:
    version: str
    status: str
    import_id: str = ""
    accepted_at: str = ""
    source_key: str = ""
    vector_keys_key: str = ""
    chunk_count: int = 0
    embedding_model: str = ""
    index_name: str = ""
    indexed_at: str = ""


def document_pk(school_id: str, document_id: str) -> str:
    return f"SCHOOL#{school_id}#DOC#{document_id}"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _key(school_id: str, document_id: str, sk: str) -> dict[str, Any]:
    return {"PK": {"S": document_pk(school_id, document_id)}, "SK": {"S": sk}}


def _values(values: dict[str, Any]) -> dict[str, Any]:
    return {k: _serializer.serialize(v) for k, v in values.items()}


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


class Registry:
    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    # --- reads --------------------------------------------------------------------------

    def get_document(self, school_id: str, document_id: str) -> DocumentRecord | None:
        item = self._get(school_id, document_id, "META")
        if item is None:
            return None
        return DocumentRecord(
            school_id=item["school_id"],
            document_id=item["document_id"],
            status=item["status"],
            active_version=item.get("active_version"),
            title=item.get("title", ""),
            audience=tuple(item.get("audience", [])),
            updated_at=item.get("updated_at", ""),
        )

    def get_version(self, school_id: str, document_id: str, version: str) -> VersionRecord | None:
        item = self._get(school_id, document_id, f"VER#{version}")
        if item is None:
            return None
        return VersionRecord(
            version=version,
            status=item["status"],
            import_id=item.get("import_id", ""),
            accepted_at=item.get("accepted_at", ""),
            source_key=item.get("source_key", ""),
            vector_keys_key=item.get("vector_keys_key", ""),
            chunk_count=int(item.get("chunk_count", 0)),
            embedding_model=item.get("embedding_model", ""),
            index_name=item.get("index_name", ""),
            indexed_at=item.get("indexed_at", ""),
        )

    def _get(self, school_id: str, document_id: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table, Key=_key(school_id, document_id, sk), ConsistentRead=True
        )
        item = response.get("Item")
        return _plain(item) if item else None

    # --- writes -------------------------------------------------------------------------

    def start_indexing(
        self,
        *,
        school_id: str,
        document_id: str,
        version: str,
        import_id: str,
        accepted_at: str,
        title: str,
        audience: tuple[str, ...],
        index_name: str,
        now: datetime,
    ) -> None:
        """VER -> INDEXING. META is created if this is the document's first version; an
        existing document keeps its status and active version until the swap."""
        stamp = _iso(now)
        self._update(
            school_id,
            document_id,
            f"VER#{version}",
            {
                "status": Status.INDEXING.value,
                "import_id": import_id,
                "accepted_at": accepted_at,
                "index_name": index_name,
                "updated_at": stamp,
            },
        )
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    **_key(school_id, document_id, "META"),
                    **_values(
                        {
                            "school_id": school_id,
                            "document_id": document_id,
                            "status": Status.INDEXING.value,
                            "title": title,
                            "audience": list(audience),
                            "updated_at": stamp,
                            "status_updated_at": f"{Status.INDEXING.value}#{stamp}",
                        }
                    ),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def set_version_fields(
        self, school_id: str, document_id: str, version: str, fields: dict[str, Any]
    ) -> None:
        self._update(school_id, document_id, f"VER#{version}", fields)

    def set_version_status(
        self, school_id: str, document_id: str, version: str, status: Status, now: datetime
    ) -> None:
        self._update(
            school_id,
            document_id,
            f"VER#{version}",
            {"status": status.value, "updated_at": _iso(now)},
        )

    def mark_document_failed_if_never_active(
        self, school_id: str, document_id: str, now: datetime
    ) -> None:
        """A first upload that fails leaves the document FAILED; a failed re-upload keeps
        the document ACTIVE on its previous version."""
        stamp = _iso(now)
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_key(school_id, document_id, "META"),
                UpdateExpression="SET #s = :failed, updated_at = :now, status_updated_at = :su",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(active_version)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=_values(
                    {":failed": "FAILED", ":now": stamp, ":su": f"FAILED#{stamp}"}
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def activate(
        self,
        *,
        school_id: str,
        document_id: str,
        version: str,
        expected_active: str | None,
        title: str,
        audience: tuple[str, ...],
        version_fields: dict[str, Any],
        now: datetime,
    ) -> None:
        """Atomically make ``version`` the active one, only if META.active_version is still
        ``expected_active`` (protects against two versions finishing out of order)."""
        stamp = _iso(now)
        if expected_active is None:
            condition = "attribute_not_exists(active_version)"
            condition_values: dict[str, Any] = {}
        else:
            condition = "active_version = :expected"
            condition_values = {":expected": expected_active}
        names, values, sets = {"#s": "status"}, {}, []
        for i, (field, value) in enumerate({**version_fields, "updated_at": stamp}.items()):
            names[f"#f{i}"] = field
            values[f":f{i}"] = value
            sets.append(f"#f{i} = :f{i}")
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": _key(school_id, document_id, "META"),
                            "UpdateExpression": (
                                "SET active_version = :v, #s = :active, title = :t, "
                                "audience = :a, updated_at = :now, status_updated_at = :su"
                            ),
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": _values(
                                {
                                    ":v": version,
                                    ":active": Status.ACTIVE.value,
                                    ":t": title,
                                    ":a": list(audience),
                                    ":now": stamp,
                                    ":su": f"{Status.ACTIVE.value}#{stamp}",
                                    **condition_values,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": _key(school_id, document_id, f"VER#{version}"),
                            "UpdateExpression": "SET #s = :active, " + ", ".join(sets),
                            "ConditionExpression": "attribute_exists(PK)",
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": _values(
                                {":active": Status.ACTIVE.value, **values}
                            ),
                        }
                    },
                ]
            )
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if exc.response["Error"]["Code"] == "TransactionCanceledException" and any(
                r.get("Code") == "ConditionalCheckFailed" for r in reasons[:1]
            ):
                raise ActivationConflictError(document_id) from None
            raise

    def _update(self, school_id: str, document_id: str, sk: str, fields: dict[str, Any]) -> None:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":f{i}": v for i, v in enumerate(fields.values())}
        self._client.update_item(
            TableName=self._table,
            Key=_key(school_id, document_id, sk),
            UpdateExpression="SET " + ", ".join(f"{n} = :{n[1:]}" for n in names),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=_values(values),
        )
