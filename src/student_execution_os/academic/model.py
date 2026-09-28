from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from student_execution_os.domain.errors import DomainError
from student_execution_os.recurrence.source import SourceSnapshot


class AcademicProviderError(DomainError):
    """A safe, stable provider failure. Messages never contain feed URLs or bodies."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AcademicProviderResult:
    snapshot: SourceSnapshot
    content_sha256: str
    component_count: int
    diagnostics: tuple[str, ...] = ()


class AcademicScheduleProvider(Protocol):
    """Fetch/read and normalize one academic source; never mutate canonical state."""

    provider_id: str

    def fetch(self) -> AcademicProviderResult:
        ...
