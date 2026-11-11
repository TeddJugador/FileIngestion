"""Validate one raw row against a dataset schema and shape the errors for the report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from ingestion.parsing.rows import RawRow, RowError, truncate_value
from ingestion.schemas.common import ValidationContext


@dataclass(frozen=True)
class ValidRow:
    line: int
    record: BaseModel


def validate_row(
    schema: type[BaseModel], row: RawRow, context: ValidationContext
) -> ValidRow | list[RowError]:
    if row.errors:  # structural problems found while parsing (e.g. extra CSV cells)
        return list(row.errors)
    try:
        record = schema.model_validate(row.data, context=context)
    except ValidationError as exc:
        return [_to_row_error(row, error) for error in exc.errors(include_url=False)]
    return ValidRow(line=row.line, record=record)


def _to_row_error(row: RawRow, error: ErrorDetails) -> RowError:
    loc = error["loc"]
    ctx = error.get("ctx") or {}
    if loc:
        field = ".".join(str(part) for part in loc)
        value: Any = "" if error["type"] == "missing" else error["input"]
    elif "field" in ctx:  # cross-field check attributed to one field
        field = str(ctx["field"])
        value = row.data.get(field, "") if isinstance(row.data, dict) else ""
    else:  # whole-record error, e.g. a JSON item that isn't an object
        field = ""
        value = row.data if not isinstance(row.data, dict) else ""
    return RowError(
        line=row.line,
        field=field,
        error_code=error["type"],
        message=error["msg"],
        value=truncate_value(value),
    )
