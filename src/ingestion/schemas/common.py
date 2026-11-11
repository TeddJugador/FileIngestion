"""Field types and helpers shared by the dataset schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, StringConstraints, ValidationInfo
from pydantic_core import PydanticCustomError

RECORD_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

StudentNumber = Annotated[str, StringConstraints(pattern=r"^STU-\d{4}-\d{5}$")]
GradeCode = Annotated[str, StringConstraints(pattern=r"^(R|[1-9]|1[0-2])$")]
TermCode = Annotated[str, StringConstraints(pattern=r"^\d{4}-T[1-4]$")]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTEGER = re.compile(r"^[+-]?\d+$")


def _iso_date_only(value: object) -> object:
    """Accept only ``YYYY-MM-DD`` strings (pydantic would also take timestamps)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()):
        return value.strip()
    raise PydanticCustomError("date_format", "Date must be in YYYY-MM-DD format")


IsoDate = Annotated[date, BeforeValidator(_iso_date_only)]


def _whole_number_only(value: object) -> int:
    """Reject anything with a decimal point, and booleans.

    Pydantic's lax int mode would accept "1500.00", 1500.0 and True.
    """
    if isinstance(value, bool):
        raise PydanticCustomError("int_type", "Input should be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
        return int(value.strip())
    raise PydanticCustomError(
        "int_no_decimals", "Input should be a whole number of cents, without decimals"
    )


WholeNumber = Annotated[int, BeforeValidator(_whole_number_only)]


@dataclass(frozen=True)
class ValidationContext:
    """Passed to ``model_validate(context=...)``.

    ``as_of`` is the upload date, used for age checks so a file validates the same way
    whenever it is (re)processed.
    """

    as_of: date

    @classmethod
    def today(cls) -> ValidationContext:
        return cls(as_of=datetime.now(UTC).date())


def context_from(info: ValidationInfo) -> ValidationContext:
    context = info.context
    if isinstance(context, ValidationContext):
        return context
    return ValidationContext.today()


def cross_field_error(error_type: str, message: str, field: str) -> PydanticCustomError:
    """A model-level error attributed to one field (``ctx.field``) for the error report."""
    return PydanticCustomError(error_type, message, {"field": field})
