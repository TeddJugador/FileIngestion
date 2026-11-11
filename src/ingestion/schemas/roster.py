from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BaseModel,
    EmailStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ingestion.schemas.common import (
    RECORD_CONFIG,
    GradeCode,
    IsoDate,
    StudentNumber,
    context_from,
    cross_field_error,
)

MIN_AGE = 4
MAX_AGE = 21

Name = Annotated[str, StringConstraints(min_length=1, max_length=60)]
ClassCode = Annotated[str, StringConstraints(pattern=r"^(R|[1-9]|1[0-2])[A-Z]$")]
E164Phone = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{6,14}$")]


class RosterRecord(BaseModel):
    model_config = RECORD_CONFIG

    student_number: StudentNumber
    first_name: Name
    last_name: Name
    date_of_birth: IsoDate
    grade_code: GradeCode
    class_code: ClassCode
    guardian_email: EmailStr
    guardian_phone: E164Phone

    @field_validator("date_of_birth")
    @classmethod
    def _age_in_range(cls, value: IsoDate, info: ValidationInfo) -> IsoDate:
        as_of = context_from(info).as_of
        age = as_of.year - value.year - ((as_of.month, as_of.day) < (value.month, value.day))
        if not MIN_AGE <= age <= MAX_AGE:
            raise PydanticCustomError(
                "age_out_of_range",
                "Age at upload must be between {min_age} and {max_age}",
                {"min_age": MIN_AGE, "max_age": MAX_AGE},
            )
        return value

    @model_validator(mode="after")
    def _class_matches_grade(self) -> Self:
        if self.class_code[:-1] != self.grade_code:
            raise cross_field_error(
                "class_grade_mismatch",
                "class_code must start with the grade_code",
                field="class_code",
            )
        return self
