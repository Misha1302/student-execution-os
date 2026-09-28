"""Provider-independent academic schedule import boundary (schema v24)."""

from .ical import ICalendarAcademicProvider, parse_icalendar
from .model import (
    AcademicProviderError,
    AcademicProviderResult,
    AcademicScheduleProvider,
)

__all__ = [
    "AcademicProviderError",
    "AcademicProviderResult",
    "AcademicScheduleProvider",
    "AcademicScheduleService",
    "ICalendarAcademicProvider",
    "parse_icalendar",
    "refresh_due_academic_schedules",
]
from .service import AcademicScheduleService, refresh_due_academic_schedules
