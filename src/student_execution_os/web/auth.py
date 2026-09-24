from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import DomainError, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository


DEFAULT_CORS_ORIGINS = (
    # Capacitor Android WebView origins (androidScheme https / http).
    "https://localhost",
    "http://localhost",
    "capacitor://localhost",
)

_LOGIN_RE = re.compile(r"^[a-z0-9._@+-]{3,64}$")
_MIN_PASSWORD = 8
_MAX_PASSWORD = 256
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32


class Unauthenticated(DomainError):
    """Request carries no valid session token."""


class RateLimited(DomainError):
    """Too many authentication attempts from one client or for one login."""


@dataclass(frozen=True)
class AuthConfig:
    registration_open: bool = True
    session_ttl: timedelta = timedelta(days=30)
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS


@dataclass(frozen=True)
class Session:
    account_id: str
    user_id: str
    login: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session: Session


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def normalize_login(login: str) -> str:
    value = (login or "").strip().lower()
    if not _LOGIN_RE.fullmatch(value):
        raise ValidationError("login must be 3-64 characters: letters, digits and . _ @ + -")
    return value


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Spent on unknown logins so response timing does not reveal which logins exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_password(password: str) -> str:
    if not isinstance(password, str) or not (_MIN_PASSWORD <= len(password) <= _MAX_PASSWORD):
        raise ValidationError(f"password must be {_MIN_PASSWORD}-{_MAX_PASSWORD} characters")
    return password


@dataclass
class AttemptLimiter:
    """In-process sliding-window limiter for authentication attempts."""

    limit: int
    window_seconds: float
    _hits: dict[str, deque] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                return
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                raise RateLimited("too many attempts; try again later")

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, deque()).append(time.monotonic())


class SQLiteAuthStore:
    """Login credentials and opaque bearer sessions.

    Tokens are random and only their SHA-256 is persisted. A session binds exactly one
    account; the client never supplies an account id.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        config: AuthConfig,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = str(database)
        self.config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.ip_limiter = AttemptLimiter(limit=30, window_seconds=600)
        self.login_limiter = AttemptLimiter(limit=8, window_seconds=600)

    def _repo(self) -> SQLiteCanonicalRepository:
        repo = SQLiteCanonicalRepository(self.database, clock=FrozenClock(self._now()))
        repo.initialize()
        return repo

    def _issue(self, conn: sqlite3.Connection, *, account_id: str, user_id: str, login: str, device_label: str | None) -> IssuedSession:
        now = self._now()
        token = secrets.token_urlsafe(32)
        expires = now + self.config.session_ttl
        conn.execute(
            "INSERT INTO auth_sessions(token_hash,account_id,user_id,device_label,created_at,expires_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (_token_hash(token), account_id, user_id, (device_label or None) and str(device_label)[:80],
             _iso(now), _iso(expires), _iso(now)),
        )
        return IssuedSession(token=token, session=Session(account_id, user_id, login, expires))

    def register(self, login: str, password: str, *, client_ip: str, device_label: str | None = None) -> IssuedSession:
        if not self.config.registration_open:
            raise Unauthenticated("registration is closed on this server")
        self.ip_limiter.check(client_ip)
        self.ip_limiter.hit(client_ip)
        login = normalize_login(login)
        password_hash = hash_password(_check_password(password))
        account_id = str(uuid4())
        user_id = str(uuid4())
        with self._repo() as repo:
            try:
                with repo._tx() as conn:
                    conn.execute("INSERT INTO accounts(id) VALUES (?)", (account_id,))
                    conn.execute(
                        "INSERT INTO auth_users(id,account_id,login,password_hash,created_at) VALUES (?,?,?,?,?)",
                        (user_id, account_id, login, password_hash, _iso(self._now())),
                    )
                    return self._issue(conn, account_id=account_id, user_id=user_id, login=login, device_label=device_label)
            except sqlite3.IntegrityError as exc:
                raise ValidationError("login is already taken") from exc

    def login(self, login: str, password: str, *, client_ip: str, device_label: str | None = None) -> IssuedSession:
        login = (login or "").strip().lower()
        self.ip_limiter.check(client_ip)
        self.login_limiter.check(login)
        with self._repo() as repo:
            row = repo.connection.execute(
                "SELECT id,account_id,password_hash FROM auth_users WHERE login=?", (login,)
            ).fetchone()
            ok = verify_password(str(password or ""), row["password_hash"] if row else _DUMMY_HASH) and row is not None
            if not ok:
                self.ip_limiter.hit(client_ip)
                self.login_limiter.hit(login)
                raise Unauthenticated("invalid login or password")
            with repo._tx() as conn:
                return self._issue(conn, account_id=row["account_id"], user_id=row["id"], login=login, device_label=device_label)

    def authenticate(self, token: str | None) -> Session:
        if not token:
            raise Unauthenticated("authentication required")
        now = self._now()
        with self._repo() as repo:
            row = repo.connection.execute(
                "SELECT s.token_hash,s.account_id,s.user_id,s.expires_at,s.last_seen_at,u.login "
                "FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.revoked_at IS NULL",
                (_token_hash(token),),
            ).fetchone()
            if row is None or _parse(row["expires_at"]) <= now:
                raise Unauthenticated("session is invalid or expired")
            expires = _parse(row["expires_at"])
            if now - _parse(row["last_seen_at"]) > timedelta(hours=1):
                expires = now + self.config.session_ttl
                with repo._tx() as conn:
                    conn.execute(
                        "UPDATE auth_sessions SET last_seen_at=?,expires_at=? WHERE token_hash=?",
                        (_iso(now), _iso(expires), row["token_hash"]),
                    )
            return Session(row["account_id"], row["user_id"], row["login"], expires)

    def logout(self, token: str) -> None:
        with self._repo() as repo, repo._tx() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (_iso(self._now()), _token_hash(token)),
            )
