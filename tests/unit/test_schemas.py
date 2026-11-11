from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from ingestion.parsing.rows import RawRow, RowError
from ingestion.schemas.common import ValidationContext
from ingestion.schemas.fee_schedule import FeeScheduleRecord
from ingestion.schemas.marks import MarksRecord
from ingestion.schemas.roster import RosterRecord
from ingestion.schemas.validation import ValidRow, validate_row


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "student_number": "STU-2026-00412",
        "first_name": "Thandiwe",
        "last_name": "Mokoena",
        "date_of_birth": "2011-04-17",
        "grade_code": "10",
        "class_code": "10B",
        "guardian_email": "n.mokoena@example.org",
        "guardian_phone": "+27821234567",
    }
    return {**base, **overrides}


def _marks(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "student_number": "STU-2026-00412",
        "assessment_code": "MATHS-TEST-3",
        "term_code": "2026-T4",
        "mark": "42.5",
        "max_mark": "50",
    }
    return {**base, **overrides}


def _fees(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "grade_code": "10",
        "term_code": "2027-T1",
        "fee_item": "Tuition",
        "amount_cents": "1250000",
        "currency": "ZAR",
    }
    return {**base, **overrides}


def _valid(schema: type[BaseModel], data: Any, context: ValidationContext) -> BaseModel:
    result = validate_row(schema, RawRow(line=2, data=data), context)
    assert isinstance(result, ValidRow), result
    return result.record


def _errors(schema: type[BaseModel], data: Any, context: ValidationContext) -> list[RowError]:
    result = validate_row(schema, RawRow(line=7, data=data), context)
    assert isinstance(result, list), result
    assert all(e.line == 7 for e in result)
    return result


def _single(schema: type[BaseModel], data: Any, context: ValidationContext) -> RowError:
    errors = _errors(schema, data, context)
    assert len(errors) == 1, errors
    return errors[0]


# --- roster ------------------------------------------------------------------------------


def test_valid_roster_row_is_trimmed(context: ValidationContext) -> None:
    record = _valid(RosterRecord, _row(first_name="  Thandiwe "), context)
    assert isinstance(record, RosterRecord)
    assert record.first_name == "Thandiwe"


def test_grade_r_roster_row(context: ValidationContext) -> None:
    _valid(RosterRecord, _row(grade_code="R", class_code="RA", date_of_birth="2021-01-30"), context)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("student_number", "STU-26-412", "string_pattern_mismatch"),
        ("first_name", "", "string_too_short"),
        ("first_name", "   ", "string_too_short"),
        ("last_name", "x" * 61, "string_too_long"),
        ("date_of_birth", "17/03/2011", "date_format"),
        ("date_of_birth", "1700000000", "date_format"),  # pydantic would read a timestamp
        ("date_of_birth", "2011-02-30", "date_from_datetime_parsing"),
        ("grade_code", "13", "string_pattern_mismatch"),
        ("grade_code", "01", "string_pattern_mismatch"),
        ("class_code", "10b", "string_pattern_mismatch"),
        ("guardian_email", "not-an-email", "value_error"),
        ("guardian_phone", "0821234567", "string_pattern_mismatch"),
        ("guardian_phone", "+0821234567", "string_pattern_mismatch"),
    ],
)
def test_roster_field_rules(field: str, value: str, code: str, context: ValidationContext) -> None:
    error = _single(RosterRecord, _row(**{field: value}), context)
    assert (error.field, error.error_code) == (field, code)
    assert error.value == value


@pytest.mark.parametrize(
    ("dob", "ok"),
    [
        ("2022-11-11", True),  # exactly 4 on the upload date
        ("2022-11-12", False),  # 4 tomorrow
        ("2005-11-12", True),  # 20, turns 21 tomorrow
        ("2004-11-12", True),  # 21 until tomorrow
        ("2004-11-11", False),  # turns 22 on the upload date
        ("2030-01-01", False),  # future
    ],
)
def test_age_is_checked_at_the_upload_date(dob: str, ok: bool, context: ValidationContext) -> None:
    data = _row(date_of_birth=dob, grade_code="R", class_code="RA")
    if ok:
        _valid(RosterRecord, data, context)
    else:
        assert _single(RosterRecord, data, context).error_code == "age_out_of_range"


def test_class_must_match_grade(context: ValidationContext) -> None:
    error = _single(RosterRecord, _row(class_code="9B"), context)
    assert (error.field, error.error_code, error.value) == (
        "class_code",
        "class_grade_mismatch",
        "9B",
    )


def test_missing_and_unknown_fields(context: ValidationContext) -> None:
    data = _row(nickname="Thandi")
    del data["guardian_phone"]
    errors = {(e.field, e.error_code): e for e in _errors(RosterRecord, data, context)}
    assert set(errors) == {("guardian_phone", "missing"), ("nickname", "extra_forbidden")}
    assert errors[("guardian_phone", "missing")].value == ""


# --- marks --------------------------------------------------------------------------------


def test_marks_are_decimal(context: ValidationContext) -> None:
    record = _valid(MarksRecord, _marks(mark=Decimal("42.5"), max_mark=50), context)
    assert isinstance(record, MarksRecord)
    assert record.mark == Decimal("42.5")
    assert isinstance(record.mark, Decimal)


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"mark": "-1"}, "mark", "greater_than_equal"),
        ({"max_mark": "0"}, "max_mark", "greater_than"),
        ({"max_mark": "1000.01"}, "max_mark", "less_than_equal"),
        ({"mark": "NaN"}, "mark", "finite_number"),
        ({"mark": "abc"}, "mark", "decimal_parsing"),
        ({"mark": "51", "max_mark": "50"}, "mark", "mark_exceeds_max"),
        ({"term_code": "2026-T5"}, "term_code", "string_pattern_mismatch"),
        ({"assessment_code": "x" * 41}, "assessment_code", "string_too_long"),
    ],
)
def test_marks_rules(
    overrides: dict[str, Any], field: str, code: str, context: ValidationContext
) -> None:
    error = _single(MarksRecord, _marks(**overrides), context)
    assert (error.field, error.error_code) == (field, code)


def test_mark_equal_to_max_is_allowed(context: ValidationContext) -> None:
    _valid(MarksRecord, _marks(mark="1000", max_mark="1000"), context)


# --- fee schedule ----------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["1250000", " 1250000 ", 1250000])
def test_whole_number_amounts(amount: Any, context: ValidationContext) -> None:
    record = _valid(FeeScheduleRecord, _fees(amount_cents=amount), context)
    assert isinstance(record, FeeScheduleRecord)
    assert record.amount_cents == 1250000


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("1250000.00", "int_no_decimals"),
        ("12500.50", "int_no_decimals"),
        (1250000.0, "int_no_decimals"),
        (Decimal("1250000.0"), "int_no_decimals"),
        (True, "int_type"),
        ("0", "greater_than"),
        ("-5", "greater_than"),
        ("R100", "int_no_decimals"),
    ],
)
def test_amounts_reject_decimals(amount: Any, code: str, context: ValidationContext) -> None:
    error = _single(FeeScheduleRecord, _fees(amount_cents=amount), context)
    assert (error.field, error.error_code) == ("amount_cents", code)


@pytest.mark.parametrize("currency", ["USD", "zar", ""])
def test_currency_must_be_zar(currency: str, context: ValidationContext) -> None:
    error = _single(FeeScheduleRecord, _fees(currency=currency), context)
    assert (error.field, error.error_code) == ("currency", "literal_error")


# --- error shaping ------------------------------------------------------------------


def test_values_are_truncated_to_100_chars(context: ValidationContext) -> None:
    error = _single(RosterRecord, _row(first_name="y" * 500), context)
    assert error.value == "y" * 100


def test_non_object_json_item_is_a_whole_row_error(context: ValidationContext) -> None:
    error = _single(MarksRecord, [1, 2, 3], context)
    assert (error.field, error.error_code) == ("", "model_type")


def test_whole_row_error_never_dumps_the_record(context: ValidationContext) -> None:
    # A model-level error without a field must not copy the whole (personal) record.
    errors = _errors(RosterRecord, _row(class_code="9B"), context)
    assert all("Mokoena" not in e.value for e in errors)


def test_report_shape(context: ValidationContext) -> None:
    error = _single(MarksRecord, _marks(mark="-1"), context)
    assert error.to_report() == {
        "line": 7,
        "field": "mark",
        "errorCode": "greater_than_equal",
        "message": error.message,
        "value": "-1",
    }


def test_records_are_immutable(context: ValidationContext) -> None:
    record = _valid(MarksRecord, _marks(), context)
    with pytest.raises(ValueError, match="frozen"):
        record.mark = Decimal(1)  # type: ignore[misc]


def test_default_context_is_today() -> None:
    assert RosterRecord.model_validate(_row()).student_number == "STU-2026-00412"
