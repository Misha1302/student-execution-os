"""Per-account LLM access: where the credentials for an account's Assistant come from.

Two sources exist, chosen per account on every request:

* ``USER_BYOK`` — the account holder saved their own provider key in Settings → AI.
  This is the default product model: the operator does not pay for inference.
* ``PLATFORM_MANAGED`` — the account holds an active entitlement (a future paid
  plan) and the operator configured platform credentials (``SEOS_PLATFORM_LLM_*``).
  The user enters nothing.

Without either, the Assistant runs the deterministic local parser; an LLM is an
enhancement, never a precondition for creating tasks.

Stored keys are encrypted with AES-256-GCM under a master key kept outside the
database (``SEOS_CREDENTIAL_KEY_FILE`` or ``SEOS_CREDENTIAL_KEY``). The associated
data binds each ciphertext to its account, provider and API address, so a row moved
to another account or re-pointed at another host does not decrypt. The API never
returns a stored key: reads expose only a masked hint such as ``sk-••••abcd``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from student_execution_os.domain.errors import (
    EntityNotFound,
    UnsupportedCapability,
    ValidationError,
    VersionConflict,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .providers import (
    PROVIDERS,
    ProviderUnavailable,
    assert_public_base_url,
    build_provider,
    normalize_provider,
    platform_provider_from_environment,
)


def _passed_before(reason: str) -> list[str]:
    """Steps a failed test still proved (a refused model means the key was accepted)."""
    reached = {"AUTH": 0, "ENDPOINT": 0, "BLOCKED_URL": 0, "NETWORK": 0, "UPSTREAM": 0, "MALFORMED": 1,
               "NOT_FOUND": 2, "QUOTA": 1, "RATE_LIMITED": 1, "REJECTED": 2, "FORMAT": 3}.get(reason, 0)
    return list(CHECKED[:reached])


class CredentialSource(str, Enum):
    NONE = "NONE"
    USER_BYOK = "USER_BYOK"
    PLATFORM_MANAGED = "PLATFORM_MANAGED"


class CredentialUnreadable(Exception):
    """The stored ciphertext cannot be decrypted (master key missing or rotated away)."""


# Provider failure reason → persisted credential status (shown in Settings).
STATUS_BY_REASON = {
    "AUTH": "INVALID_KEY",
    "NOT_FOUND": "MODEL_NOT_FOUND",
    "ENDPOINT": "ENDPOINT_NOT_FOUND",
    "RATE_LIMITED": "RATE_LIMITED",
    "QUOTA": "QUOTA_EXCEEDED",
    "FORMAT": "UNSUPPORTED_FORMAT",
    "MALFORMED": "MALFORMED_RESPONSE",
    "REJECTED": "REJECTED",
    "UPSTREAM": "PROVIDER_ERROR",
    "NETWORK": "UNREACHABLE",
    "BLOCKED_URL": "BLOCKED_URL",
}
# Failures that say something lasting about the credential (not a passing outage or
# one odd answer); the connection test records every outcome.
_STICKY_REASONS = {"AUTH", "NOT_FOUND", "ENDPOINT", "QUOTA", "BLOCKED_URL"}
# What a passed connection test has proven, in the order it is proven.
CHECKED = ("key", "endpoint", "model", "format")

_KEY_SHAPE = re.compile(r"^[\x21-\x7e]{8,512}$")
_MODEL_SHAPE = re.compile(r"^[\x21-\x7e]{1,200}$")
_AAD_VERSION = "seos-llm-credential:v1"


def _aad(account_id: str, provider: str, base_url: str | None) -> bytes:
    return f"{_AAD_VERSION}\x1f{account_id}\x1f{provider}\x1f{base_url or ''}".encode()


def key_hint(api_key: str) -> str:
    """A masked form that identifies a key to its owner without revealing it."""
    if len(api_key) < 12:
        return "••••"
    prefix = api_key[:3] if api_key.startswith("sk-") else ""
    return f"{prefix}••••{api_key[-4:]}"


class CredentialCipher:
    """AES-256-GCM keyring. The first key encrypts; every listed key can decrypt.

    Rotation: put the new key first in the key file, keep the old one below it,
    restart, run ``python -m student_execution_os credentials-rekey``, then drop the
    old line.
    """

    def __init__(self, keys: list[bytes]) -> None:
        if not keys or any(len(key) != 32 for key in keys):
            raise ValueError("credential master keys must be 32 bytes each")
        self._keys = {self.key_id(key): key for key in keys}
        self.active_key_id = self.key_id(keys[0])

    @staticmethod
    def key_id(key: bytes) -> str:
        return hashlib.sha256(b"seos-credential-key-id" + key).hexdigest()[:16]

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

    @staticmethod
    def _decode(text: str) -> bytes:
        value = text.strip()
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except ValueError as exc:
            raise ValueError("credential master key is not base64") from exc

    @classmethod
    def from_environment(cls) -> "CredentialCipher | None":
        """The configured keyring, or ``None`` (saving keys is then unavailable).

        A missing or malformed key file degrades instead of taking the API down; the
        server reports the state at startup.
        """
        raw = os.environ.get("SEOS_CREDENTIAL_KEY", "").strip()
        path = os.environ.get("SEOS_CREDENTIAL_KEY_FILE", "").strip()
        try:
            lines: list[str] = []
            if raw:
                lines = [raw]
            elif path:
                file = Path(path)
                if not file.is_file():
                    return None
                lines = [line.strip() for line in file.read_text(encoding="utf-8").splitlines()]
            keys = [cls._decode(line) for line in lines if line and not line.startswith("#")]
            return cls(keys) if keys else None
        except (OSError, ValueError):
            logging.getLogger("student_execution_os.credentials").error(
                "credential master key is unreadable or malformed; AI keys cannot be used")
            return None

    def encrypt(self, plaintext: str, aad: bytes) -> tuple[bytes, bytes, str]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[self.active_key_id]).encrypt(nonce, plaintext.encode(), aad)
        return nonce, ciphertext, self.active_key_id

    def decrypt(self, nonce: bytes, ciphertext: bytes, key_id: str, aad: bytes) -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self._keys.get(key_id)
        if key is None:
            raise CredentialUnreadable("the master key for this credential is not configured")
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad).decode()
        except InvalidTag:
            raise CredentialUnreadable("stored credential does not authenticate") from None


class _TestLimiter:
    """Connection tests make outbound requests; keep them to a human pace per account."""

    def __init__(self, limit: int = 6, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, account_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [at for at in self._hits.get(account_id, []) if now - at < self.window]
            allowed = len(hits) < self.limit
            if allowed:
                hits.append(now)
            self._hits[account_id] = hits
            return allowed


TEST_LIMITER = _TestLimiter()


@dataclass(frozen=True)
class ResolvedLlm:
    source: CredentialSource
    provider: Any | None
    status: str | None = None  # stored credential status for USER_BYOK

    @property
    def live(self) -> bool:
        return self.provider is not None


class LlmCredentialStore:
    """Account-scoped credential and entitlement access. Every query filters by account."""

    def __init__(self, repo: SQLiteCanonicalRepository, cipher: CredentialCipher | None = None, *,
                 platform: Any | None = None, use_environment: bool = True) -> None:
        self.repo = repo
        self.cipher = cipher if cipher is not None or not use_environment else CredentialCipher.from_environment()
        self._platform = platform
        self._use_environment = use_environment

    # ---- reads ---------------------------------------------------------------------

    def _row(self, account_id: str):
        return self.repo.connection.execute(
            "SELECT * FROM llm_credentials WHERE account_id=?", (account_id,)
        ).fetchone()

    def platform_provider(self):
        if self._platform is not None or not self._use_environment:
            return self._platform
        try:
            return platform_provider_from_environment()
        except ValidationError:
            return None

    def entitlement(self, account_id: str) -> dict[str, Any] | None:
        row = self.repo.connection.execute(
            "SELECT source,plan,granted_at,expires_at FROM llm_entitlements WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        expires = _dt(row["expires_at"]) if row["expires_at"] else None
        if expires is not None and expires <= self.repo.clock.now():
            return None
        return dict(row)

    def resolve(self, account_id: str) -> ResolvedLlm:
        """The provider this account's Assistant uses right now, or the local parser."""
        row = self._row(account_id)
        if row is not None and self.cipher is not None:
            try:
                api_key = self.cipher.decrypt(row["key_nonce"], row["key_ciphertext"], row["key_id"],
                                              _aad(account_id, row["provider"], row["base_url"]))
            except CredentialUnreadable:
                self.record_status(account_id, "UNREADABLE")
            else:
                provider = build_provider(row["provider"], api_key=api_key, model=row["model"],
                                          base_url=row["base_url"], user_supplied=True)
                return ResolvedLlm(CredentialSource.USER_BYOK, provider, row["status"])
        if self.entitlement(account_id) is not None:
            platform = self.platform_provider()
            if platform is not None:
                return ResolvedLlm(CredentialSource.PLATFORM_MANAGED, platform)
        return ResolvedLlm(CredentialSource.NONE, None)

    def public(self, account_id: str) -> dict[str, Any]:
        """Settings view. Never contains the key, only its masked hint."""
        row = self._row(account_id)
        resolved = self.resolve(account_id)
        entitled = self.entitlement(account_id) is not None
        credential = None
        if row is not None:
            row = self._row(account_id)  # resolve() may have updated the status
            credential = {
                "provider": row["provider"], "model": row["model"], "base_url": row["base_url"],
                "key_hint": row["key_hint"], "status": row["status"], "last_checked_at": row["last_checked_at"],
                "updated_at": row["updated_at"], "version": int(row["version"]),
            }
        return {
            "source": resolved.source.value,
            "active": resolved.live,
            "credential": credential,
            "storage_available": self.cipher is not None,
            "platform_managed": {"entitled": entitled, "available": entitled and self.platform_provider() is not None},
            "providers": [
                {"id": kind, "label": label, "requires_base_url": default is None}
                for kind, (label, default) in PROVIDERS.items()
            ],
        }

    # ---- writes --------------------------------------------------------------------

    def save(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.cipher is None:
            raise UnsupportedCapability("this server is not configured to store AI keys")
        self.repo._require_account(account_id)
        provider = normalize_provider(str(payload.get("provider", "")))
        model = str(payload.get("model") or "").strip()
        if not _MODEL_SHAPE.match(model):
            raise ValidationError("model must be 1-200 visible characters without spaces")
        base_url = None
        if PROVIDERS[provider][1] is None:
            base_url = str(payload.get("base_url") or "").strip().rstrip("/")
            if not base_url or len(base_url) > 500:
                raise ValidationError("an API address is required for an OpenAI-compatible provider")
            assert_public_base_url(base_url)
        raw_key = payload.get("api_key")
        api_key = None if raw_key in (None, "") else str(raw_key).strip()
        if api_key is not None and not _KEY_SHAPE.match(api_key):
            raise ValidationError("API key must be 8-512 visible characters without spaces")
        expected = payload.get("expected_version")
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            current = conn.execute("SELECT * FROM llm_credentials WHERE account_id=?", (account_id,)).fetchone()
            if expected is not None and (current is None or int(current["version"]) != int(expected)):
                raise VersionConflict("AI settings changed on another device")
            if api_key is None:
                # Keeping the stored key is only allowed while it keeps going to the same
                # place: re-pointing a saved key at another host would hand it over.
                if current is None:
                    raise ValidationError("API key is required")
                if current["provider"] != provider or (current["base_url"] or None) != base_url:
                    raise ValidationError("enter the API key again to change the provider or API address")
                conn.execute(
                    "UPDATE llm_credentials SET model=?,status='UNTESTED',last_checked_at=NULL,updated_at=?,"
                    "version=version+1 WHERE account_id=?",
                    (model, now, account_id),
                )
            else:
                nonce, ciphertext, key_id = self.cipher.encrypt(api_key, _aad(account_id, provider, base_url))
                conn.execute(
                    "INSERT INTO llm_credentials(account_id,provider,model,base_url,key_ciphertext,key_nonce,key_id,"
                    "key_hint,status,last_checked_at,created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,'UNTESTED',NULL,?,?,1) "
                    "ON CONFLICT(account_id) DO UPDATE SET provider=excluded.provider,model=excluded.model,"
                    "base_url=excluded.base_url,key_ciphertext=excluded.key_ciphertext,key_nonce=excluded.key_nonce,"
                    "key_id=excluded.key_id,key_hint=excluded.key_hint,status='UNTESTED',last_checked_at=NULL,"
                    "updated_at=excluded.updated_at,version=llm_credentials.version+1",
                    (account_id, provider, model, base_url, ciphertext, nonce, key_id, key_hint(api_key), now, now),
                )
        return self.public(account_id)

    def delete(self, account_id: str) -> dict[str, Any]:
        with self.repo._tx() as conn:
            conn.execute("DELETE FROM llm_credentials WHERE account_id=?", (account_id,))
        return self.public(account_id)

    def record_status(self, account_id: str, status: str) -> None:
        with self.repo._tx() as conn:
            conn.execute(
                "UPDATE llm_credentials SET status=?,last_checked_at=? WHERE account_id=? AND status IS NOT ?",
                (status, _iso(self.repo.clock.now()), account_id, status),
            )

    def record_use(self, account_id: str, failure: ProviderUnavailable | None) -> None:
        """Remember what a live request said about the stored key (only lasting facts)."""
        if failure is None:
            self.record_status(account_id, "OK")
        elif failure.reason in _STICKY_REASONS:
            self.record_status(account_id, STATUS_BY_REASON[failure.reason])

    def test(self, account_id: str) -> dict[str, Any]:
        """Smoke-test the saved credential with a real interpret() round trip.

        An HTTP 2xx alone proves nothing: the probe uses the same prompt and request
        shape as capture and passes only when the model answers with a typed action.
        The result names the failed step so Settings can say what to fix.
        """
        if self._row(account_id) is None:
            raise EntityNotFound("no AI key is saved for this account")
        if not TEST_LIMITER.allow(account_id):
            from student_execution_os.web.auth import RateLimited
            raise RateLimited("too many connection tests; try again in a minute")
        resolved = self.resolve(account_id)
        if resolved.source is not CredentialSource.USER_BYOK:
            return {"ok": False, "status": "UNREADABLE", "reason": None, "http_status": None, "checked": [],
                    "latency_ms": None, "settings": self.public(account_id)}
        started = time.monotonic()
        failure: ProviderUnavailable | None = None
        try:
            resolved.provider.check(_iso(self.repo.clock.now()))
        except ProviderUnavailable as exc:
            failure = exc
        latency = round((time.monotonic() - started) * 1000)
        status = "OK" if failure is None else STATUS_BY_REASON.get(failure.reason, "UNREACHABLE")
        self.record_status(account_id, status)
        with self.repo._tx() as conn:  # a repeated test refreshes the time even if unchanged
            conn.execute("UPDATE llm_credentials SET last_checked_at=? WHERE account_id=?",
                         (_iso(self.repo.clock.now()), account_id))
        return {"ok": failure is None, "status": status,
                "reason": None if failure is None else failure.reason,
                "http_status": None if failure is None else failure.http_status,
                "checked": list(CHECKED) if failure is None else _passed_before(failure.reason),
                "latency_ms": latency, "settings": self.public(account_id)}

    # ---- operator actions ----------------------------------------------------------

    def grant_entitlement(self, account_id: str, plan: str, expires_at: datetime | None = None) -> None:
        self.repo._require_account(account_id)
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO llm_entitlements(account_id,source,plan,granted_at,expires_at) VALUES (?,'PLATFORM_MANAGED',?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET plan=excluded.plan,granted_at=excluded.granted_at,expires_at=excluded.expires_at",
                (account_id, plan, _iso(self.repo.clock.now()), _iso(expires_at)),
            )

    def revoke_entitlement(self, account_id: str) -> None:
        with self.repo._tx() as conn:
            conn.execute("DELETE FROM llm_entitlements WHERE account_id=?", (account_id,))

    def rekey(self) -> int:
        """Re-encrypt every stored key under the active master key."""
        if self.cipher is None:
            raise UnsupportedCapability("no credential master key is configured")
        rows = self.repo.connection.execute("SELECT * FROM llm_credentials WHERE key_id<>?", (self.cipher.active_key_id,)).fetchall()
        changed = 0
        with self.repo._tx() as conn:
            for row in rows:
                aad = _aad(row["account_id"], row["provider"], row["base_url"])
                try:
                    api_key = self.cipher.decrypt(row["key_nonce"], row["key_ciphertext"], row["key_id"], aad)
                except CredentialUnreadable:
                    continue
                nonce, ciphertext, key_id = self.cipher.encrypt(api_key, aad)
                conn.execute("UPDATE llm_credentials SET key_ciphertext=?,key_nonce=?,key_id=? WHERE account_id=?",
                             (ciphertext, nonce, key_id, row["account_id"]))
                changed += 1
        return changed
