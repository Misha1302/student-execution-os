from __future__ import annotations

from student_execution_os.domain.errors import DomainError, ValidationError, VersionConflict


class SharedVersionConflict(VersionConflict):
    """``expected_version`` of a versioned group entity is stale; carries the current one."""

    def __init__(self, entity: str, expected: int | None, current: int) -> None:
        super().__init__(f"{entity} version is {current}, not {expected}")
        self.current_version = current


class GroupRateLimited(DomainError):
    """Too many group-wide actions by one member in a short time (anti-spam)."""


class ExternalOwnedField(ValidationError):
    """The field belongs to the external source of a bound event, not to the group."""


class InviteUnavailable(ValidationError):
    """The invite is unknown, revoked, expired or used up (never says which)."""
