from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ingestion.schemas.common import RECORD_CONFIG, StudentNumber, TermCode, cross_field_error

MAX_MAX_MARK = Decimal(1000)

AssessmentCode = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class MarksRecord(BaseModel):
    model_config = RECORD_CONFIG

    student_number: StudentNumber
    assessment_code: AssessmentCode
    term_code: TermCode
    # Decimal, never float. allow_inf_nan is False for Decimal by default.
    mark: Annotated[Decimal, Field(ge=0)]
    max_mark: Annotated[Decimal, Field(gt=0, le=MAX_MAX_MARK)]

    @model_validator(mode="after")
    def _mark_within_max(self) -> Self:
        if self.mark > self.max_mark:
            raise cross_field_error(
                "mark_exceeds_max", "mark must not exceed max_mark", field="mark"
            )
        return self
