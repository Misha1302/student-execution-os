from __future__ import annotations

import os
from pathlib import Path

from student_execution_os.agent.credentials import CredentialCipher, CredentialUnreadable

_AAD_VERSION = "seos-academic-feed:v1"


def feed_aad(account_id: str, connector_id: str) -> bytes:
    return f"{_AAD_VERSION}\x1f{account_id}\x1f{connector_id}".encode()


def academic_feed_cipher_from_environment() -> CredentialCipher | None:
    """Dedicated keyring: the background importer never receives LLM credentials."""
    raw = os.environ.get("SEOS_ACADEMIC_FEED_KEY", "").strip()
    path = os.environ.get("SEOS_ACADEMIC_FEED_KEY_FILE", "").strip()
    try:
        lines: list[str] = []
        if raw:
            lines = [raw]
        elif path:
            file = Path(path)
            if not file.is_file():
                return None
            lines = [line.strip() for line in file.read_text(encoding="utf-8").splitlines()]
        keys = [CredentialCipher._decode(line) for line in lines if line and not line.startswith("#")]
        return CredentialCipher(keys) if keys else None
    except (OSError, ValueError):
        return None


__all__ = ["CredentialUnreadable", "academic_feed_cipher_from_environment", "feed_aad"]
