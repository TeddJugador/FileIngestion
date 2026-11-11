"""Dataset registry: the single place that maps upload paths to datasets.

Path segment (``marks``) <-> event/DynamoDB name (``MARKS``), allowed extensions,
size limits, pydantic schema and natural key all live here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel

from ingestion.errors import ReasonCode
from ingestion.schemas.fee_schedule import FeeScheduleRecord
from ingestion.schemas.marks import MarksRecord
from ingestion.schemas.roster import RosterRecord

RAW_PREFIX = "raw/"
SCHOOL_ID_PATTERN = re.compile(r"^SCH-\d{3}$")
UNKNOWN = "UNKNOWN"  # sentinel for datasetType / metadata.schoolId on ImportFailed

DEFAULT_MAX_TABULAR_BYTES = 20 * 1024 * 1024  # 20971520
DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # 26214400

TABULAR_EXTENSIONS = frozenset({"csv", "json", "jsonl"})
DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx", "md", "txt"})


class DatasetKind(StrEnum):
    TABULAR = "TABULAR"
    DOCUMENT = "DOCUMENT"


class FileFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


@dataclass(frozen=True)
class DatasetSpec:
    path_name: str  # lowercase S3 path segment, e.g. "fee_schedule"
    kind: DatasetKind
    extensions: frozenset[str]
    size_limit_env: str
    default_max_bytes: int
    schema: type[BaseModel] | None = None
    natural_key: Callable[[BaseModel], str] | None = None

    @property
    def event_name(self) -> str:
        """``datasetType`` in events and ``<DATASET>`` in DynamoDB keys."""
        return self.path_name.upper()

    def max_bytes(self) -> int:
        return int(os.environ.get(self.size_limit_env, self.default_max_bytes))


def _roster_key(record: BaseModel) -> str:
    assert isinstance(record, RosterRecord)
    return record.student_number


def _marks_key(record: BaseModel) -> str:
    assert isinstance(record, MarksRecord)
    # assessment_code is free text, but student_number and term_code can't contain "#",
    # so the key still splits unambiguously from both ends.
    return f"{record.student_number}#{record.assessment_code}#{record.term_code}"


def _fee_schedule_key(record: BaseModel) -> str:
    assert isinstance(record, FeeScheduleRecord)
    return f"{record.grade_code}#{record.term_code}#{record.fee_item}"


def _tabular(
    path_name: str, schema: type[BaseModel], key: Callable[[BaseModel], str]
) -> DatasetSpec:
    return DatasetSpec(
        path_name=path_name,
        kind=DatasetKind.TABULAR,
        extensions=TABULAR_EXTENSIONS,
        size_limit_env="MAX_TABULAR_BYTES",
        default_max_bytes=DEFAULT_MAX_TABULAR_BYTES,
        schema=schema,
        natural_key=key,
    )


ROSTER = _tabular("roster", RosterRecord, _roster_key)
MARKS = _tabular("marks", MarksRecord, _marks_key)
FEE_SCHEDULE = _tabular("fee_schedule", FeeScheduleRecord, _fee_schedule_key)
DOCUMENTS = DatasetSpec(
    path_name="documents",
    kind=DatasetKind.DOCUMENT,
    extensions=DOCUMENT_EXTENSIONS,
    size_limit_env="MAX_DOCUMENT_BYTES",
    default_max_bytes=DEFAULT_MAX_DOCUMENT_BYTES,
)

DATASETS: Mapping[str, DatasetSpec] = MappingProxyType(
    {spec.path_name: spec for spec in (ROSTER, MARKS, FEE_SCHEDULE, DOCUMENTS)}
)


def dataset_for_path_name(path_name: str) -> DatasetSpec | None:
    """Exact, case-sensitive lookup: the path segment must be lowercase."""
    return DATASETS.get(path_name)


# --- upload key routing ------------------------------------------------------------


@dataclass(frozen=True)
class UploadKey:
    """A key that passed routing: every component is validated and safe to reuse."""

    key: str
    dataset: DatasetSpec
    school_id: str
    relative_path: str  # path after raw/<dataset>/<school_id>/
    extension: str  # lowercase, without the dot

    @property
    def file_format(self) -> FileFormat | None:
        return FileFormat(self.extension) if self.extension in TABULAR_EXTENSIONS else None


@dataclass(frozen=True)
class RejectedKey:
    """A key that failed routing. ``dataset``/``school_id`` are set only when valid,
    which decides whether the summary goes under the dataset path or ``_rejected/``."""

    key: str
    reason: ReasonCode
    detail: str
    dataset: DatasetSpec | None
    school_id: str | None

    @property
    def components_trusted(self) -> bool:
        return self.dataset is not None and self.school_id is not None


def parse_upload_key(key: str) -> UploadKey | RejectedKey:
    """Route ``raw/<dataset_type>/<school_id>/<filename>.<ext>``.

    The key must already be URL-decoded. Tabular datasets allow no subfolders after
    the school ID; documents do. Extensions are case-sensitive, matching the S3
    notification suffix filters (``.CSV`` never triggers a notification anyway).
    """

    def reject(
        detail: str, dataset: DatasetSpec | None = None, school: str | None = None
    ) -> RejectedKey:
        return RejectedKey(key, ReasonCode.UNSUPPORTED_FORMAT, detail, dataset, school)

    if not key.startswith(RAW_PREFIX):
        return reject("key is not under raw/")
    parts = key[len(RAW_PREFIX) :].split("/")
    if len(parts) < 3:
        return reject("key must be raw/<dataset_type>/<school_id>/<filename>")

    dataset_segment, school_segment, *rest = parts
    dataset = dataset_for_path_name(dataset_segment)
    school_id = school_segment if SCHOOL_ID_PATTERN.fullmatch(school_segment) else None

    if dataset is None:
        return reject("unsupported dataset type", None, school_id)
    if school_id is None:
        return reject("invalid school ID", dataset, None)

    if any(segment == "" for segment in rest):
        return reject("empty path segment", dataset, school_id)
    if dataset.kind is DatasetKind.TABULAR and len(rest) != 1:
        return reject("tabular uploads may not use subfolders", dataset, school_id)

    filename = rest[-1]
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem:
        return reject("file name has no extension or no name", dataset, school_id)
    if extension not in dataset.extensions:
        return reject("extension not allowed for this dataset", dataset, school_id)

    return UploadKey(
        key=key,
        dataset=dataset,
        school_id=school_id,
        relative_path="/".join(rest),
        extension=extension,
    )
