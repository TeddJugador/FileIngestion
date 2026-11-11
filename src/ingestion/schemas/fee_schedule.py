from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from ingestion.schemas.common import RECORD_CONFIG, GradeCode, TermCode, WholeNumber

FeeItem = Annotated[str, StringConstraints(min_length=1, max_length=60)]


class FeeScheduleRecord(BaseModel):
    model_config = RECORD_CONFIG

    grade_code: GradeCode
    term_code: TermCode
    fee_item: FeeItem
    amount_cents: Annotated[WholeNumber, Field(gt=0)]
    currency: Literal["ZAR"]  # v1 only
