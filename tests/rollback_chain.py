"""Apply the documented rollback scripts from the current schema down to a version.

Each stage adds ``persistence/rollback/NNN_*_down.sql``; tests of an older migration
roll a current database back through every newer script first. A missing script for
a version in the range is an error (every version above 22 documents its rollback).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from student_execution_os.persistence.sqlite import SCHEMA_VERSION

ROLLBACK_DIR = Path("src/student_execution_os/persistence/rollback")


def rollback_script(version: int) -> Path:
    matches = sorted(ROLLBACK_DIR.glob(f"{version:03d}_*_down.sql"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected exactly one rollback script for v{version}")
    return matches[0]


def roll_back_newer_than(conn: sqlite3.Connection, version: int) -> None:
    """Run the rollback scripts for SCHEMA_VERSION .. version+1, newest first."""
    for current in range(SCHEMA_VERSION, version, -1):
        conn.executescript(rollback_script(current).read_text(encoding="utf-8"))
