"""Persistence adapters for canonical local state."""

from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository

__all__ = ["SCHEMA_VERSION", "SQLiteCanonicalRepository"]
