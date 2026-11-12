"""Who is calling, taken from the verified JWT only (never from the request body)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SCHOOL_ID = re.compile(r"^SCH-\d{3}$")


class Role(StrEnum):
    ADMIN = "ADMIN"
    STAFF = "STAFF"
    PARENT = "PARENT"


# Highest privilege first: a user in several groups acts with the strongest role.
_GROUP_ROLES = (("admin", Role.ADMIN), ("staff", Role.STAFF), ("parent", Role.PARENT))
AUDIENCES = {
    Role.PARENT: ("PARENT",),
    Role.STAFF: ("STAFF", "PARENT"),
    Role.ADMIN: ("STAFF", "PARENT"),
}


class NotAuthorizedError(Exception):
    """The token is valid but doesn't carry what this API needs (school, group)."""


@dataclass(frozen=True)
class Caller:
    user_id: str  # Cognito "sub": stable even if the email changes
    school_id: str
    role: Role

    @property
    def audiences(self) -> tuple[str, ...]:
        return AUDIENCES[self.role]

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


def parse_groups(value: Any) -> set[str]:  # noqa: ANN401 - raw JWT claim
    """API Gateway HTTP APIs pass array claims as a string like "[admin staff]"."""
    if isinstance(value, list):
        return {str(v).strip().lower() for v in value}
    if isinstance(value, str):
        return {g.lower() for g in re.split(r"[\s,]+", value.strip("[] ")) if g}
    return set()


def caller_from_claims(claims: Mapping[str, Any]) -> Caller:
    user_id = str(claims.get("sub", ""))
    school_id = str(claims.get("custom:school_id", ""))
    groups = parse_groups(claims.get("cognito:groups"))
    if not user_id or not SCHOOL_ID.fullmatch(school_id):
        raise NotAuthorizedError("token has no valid school")
    for group, role in _GROUP_ROLES:
        if group in groups:
            return Caller(user_id=user_id, school_id=school_id, role=role)
    raise NotAuthorizedError("user is in no recognised group")
