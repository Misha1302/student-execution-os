"""OAuth 2.1 authorization server for connecting MCP clients (ChatGPT, Codex, ...).

Deliberately small and aligned with the MCP authorization spec: protected-resource and
authorization-server metadata, dynamic client registration for *public* clients, the
authorization-code grant with mandatory PKCE S256, and consent given inside the signed-in
botay! app. What an exchange returns is an ordinary capability grant token
(``capabilities.py``): one credential type, visible and revocable in Settings, with the
same scopes, expiry and canonical mutation path. There are no refresh tokens; when the
grant expires the client asks for consent again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from student_execution_os.capabilities import READ_SCOPES, SCOPES, CapabilityStore, _normalize_scopes
from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

AUTHORIZATION_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
DEFAULT_GRANT_DAYS = 90
MAX_REDIRECT_URIS = 5


class OAuthError(DomainError):
    """An RFC 6749 error: ``error`` is the protocol code, the message its description."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.status = status


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _valid_redirect_uri(uri: Any) -> bool:
    if not isinstance(uri, str) or len(uri) > 500:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.hostname or parts.username or parts.password:
        return False
    if parts.scheme == "https":
        return True
    # Native/CLI clients (Codex) receive the code on a loopback listener (RFC 8252).
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


def protected_resource_metadata(origin: str) -> dict[str, Any]:
    return {"resource": f"{origin}/mcp", "authorization_servers": [origin],
            "scopes_supported": sorted(SCOPES), "bearer_methods_supported": ["header"],
            "resource_name": "botay!"}


def authorization_server_metadata(origin: str) -> dict[str, Any]:
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}/oauth/authorize",
        "token_endpoint": f"{origin}/oauth/token",
        "registration_endpoint": f"{origin}/oauth/register",
        "authorization_response_iss_parameter_supported": True,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": sorted(SCOPES),
    }


class OAuthServer:
    def __init__(self, repo: SQLiteCanonicalRepository, *, issuer: str) -> None:
        self.repo = repo
        self.issuer = issuer

    # ---- registration --------------------------------------------------------------

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        uris = payload.get("redirect_uris")
        if (not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS
                or not all(_valid_redirect_uri(uri) for uri in uris)):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be 1-5 https (or loopback http) URLs")
        method = payload.get("token_endpoint_auth_method", "none")
        if method != "none":
            raise OAuthError("invalid_client_metadata", "only public clients (token_endpoint_auth_method none)")
        name = str(payload.get("client_name") or "").strip()[:80] or urlsplit(uris[0]).hostname or "MCP client"
        client_id = uuid4().hex
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            conn.execute("INSERT INTO oauth_clients(id,name,redirect_uris_json,created_at) VALUES (?,?,?,?)",
                         (client_id, name, json.dumps(uris), now))
        return {"client_id": client_id, "client_name": name, "redirect_uris": uris,
                "client_id_issued_at": int(self.repo.clock.now().timestamp()),
                "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
                "response_types": ["code"]}

    def _client(self, client_id: str):
        return self.repo.connection.execute("SELECT * FROM oauth_clients WHERE id=?", (client_id,)).fetchone()

    # ---- authorization -------------------------------------------------------------

    def start(self, params: dict[str, str]) -> str:
        """Validate an authorization request; return the pending request id.

        Raises ``OAuthError`` with ``status=400`` when the client or redirect URI cannot
        be trusted (never redirect then), otherwise ``redirect`` is safe to use.
        """
        client = self._client(params.get("client_id", ""))
        redirect_uri = params.get("redirect_uri", "")
        if client is None:
            raise OAuthError("invalid_client", "unknown client_id")
        if redirect_uri not in json.loads(client["redirect_uris_json"]):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
        state = params.get("state")
        if state is not None and len(state) > 500:
            raise OAuthError("invalid_request", "state is too long")
        if params.get("response_type") != "code":
            raise RedirectError(redirect_uri, "unsupported_response_type", "response_type must be code", state)
        if params.get("resource") != f"{self.issuer}/mcp":
            raise RedirectError(redirect_uri, "invalid_target", "resource must identify this MCP server", state)
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
            raise RedirectError(redirect_uri, "invalid_request", "PKCE S256 code_challenge is required", state)
        requested = [scope for scope in (params.get("scope") or "").split() if scope]
        if requested:
            unknown = sorted(set(requested) - SCOPES.keys())
            if unknown:
                raise RedirectError(redirect_uri, "invalid_scope", f"unknown scopes: {' '.join(unknown)}", state)
        else:
            requested = sorted(READ_SCOPES)
        now = self.repo.clock.now()
        request_id = secrets.token_urlsafe(24)
        with self.repo._tx() as conn:
            # Unanswered and finished requests are kept only while useful (bounded table).
            conn.execute("DELETE FROM oauth_authorizations WHERE expires_at<=? AND status IN ('PENDING','APPROVED','DENIED')",
                         (_iso(now),))
            conn.execute(
                "INSERT INTO oauth_authorizations(id,client_id,redirect_uri,code_challenge,requested_scopes_json,"
                "state,status,created_at,expires_at) VALUES (?,?,?,?,?,?,'PENDING',?,?)",
                (request_id, client["id"], redirect_uri, challenge, json.dumps(sorted(set(requested))),
                 state, _iso(now), _iso(now + AUTHORIZATION_TTL)),
            )
        return request_id

    def _pending(self, request_id: str):
        row = self.repo.connection.execute(
            "SELECT a.*, c.name AS client_name FROM oauth_authorizations a JOIN oauth_clients c ON c.id=a.client_id "
            "WHERE a.id=?", (request_id,)).fetchone()
        if row is None or row["status"] != "PENDING" or _dt(row["expires_at"]) <= self.repo.clock.now():
            raise EntityNotFound("this connection request has expired or was already answered")
        return row

    def describe(self, request_id: str) -> dict[str, Any]:
        row = self._pending(request_id)
        return {"id": row["id"], "client_name": row["client_name"],
                "redirect_host": urlsplit(row["redirect_uri"]).hostname,
                "requested_scopes": json.loads(row["requested_scopes_json"]),
                "scopes": [{"id": scope, "description": text} for scope, text in SCOPES.items()],
                "default_expires_in_days": DEFAULT_GRANT_DAYS, "expires_at": row["expires_at"]}

    def _redirect(self, row, **params: str | None) -> str:
        query = {key: value for key, value in params.items() if value is not None}
        if row["state"] is not None:
            query["state"] = row["state"]
        query["iss"] = self.issuer
        separator = "&" if urlsplit(row["redirect_uri"]).query else "?"
        return f"{row['redirect_uri']}{separator}{urlencode(query)}"

    def approve(self, request_id: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._pending(request_id)
        scopes = _normalize_scopes(payload.get("scopes"))
        days = payload.get("expires_in_days", DEFAULT_GRANT_DAYS)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            raise ValidationError("expires_in_days must be an integer from 1 to 366")
        code = secrets.token_urlsafe(32)
        now = self.repo.clock.now()
        with self.repo._tx() as conn:
            changed = conn.execute(
                "UPDATE oauth_authorizations SET status='APPROVED',account_id=?,approved_scopes_json=?,"
                "expires_in_days=?,code_hash=?,expires_at=? WHERE id=? AND status='PENDING'",
                (account_id, json.dumps(sorted(scopes)), days, _sha256(code), _iso(now + CODE_TTL), request_id),
            ).rowcount
        if not changed:
            raise EntityNotFound("this connection request has expired or was already answered")
        return {"redirect": self._redirect(row, code=code)}

    def deny(self, request_id: str, account_id: str) -> dict[str, Any]:
        row = self._pending(request_id)
        with self.repo._tx() as conn:
            conn.execute("UPDATE oauth_authorizations SET status='DENIED',account_id=? WHERE id=? AND status='PENDING'",
                         (account_id, request_id))
        return {"redirect": self._redirect(row, error="access_denied",
                                           error_description="the user declined the connection")}

    # ---- token ---------------------------------------------------------------------

    def exchange(self, form: dict[str, str]) -> dict[str, Any]:
        if form.get("grant_type") != "authorization_code":
            raise OAuthError("unsupported_grant_type", "only authorization_code is supported")
        if form.get("resource") != f"{self.issuer}/mcp":
            raise OAuthError("invalid_target", "resource must identify this MCP server")
        code, verifier = form.get("code") or "", form.get("code_verifier") or ""
        if not code or not 43 <= len(verifier) <= 128:
            raise OAuthError("invalid_request", "code and a PKCE code_verifier are required")
        now = self.repo.clock.now()
        with self.repo._tx() as conn:
            row = conn.execute("SELECT a.*, c.name AS client_name FROM oauth_authorizations a "
                               "JOIN oauth_clients c ON c.id=a.client_id WHERE a.code_hash=?",
                               (_sha256(code),)).fetchone()
            if row is None:
                raise OAuthError("invalid_grant", "unknown authorization code")
            replayed = row["status"] == "EXCHANGED"
            if not replayed and (row["status"] != "APPROVED" or _dt(row["expires_at"]) <= now
                    or form.get("client_id") != row["client_id"] or form.get("redirect_uri") != row["redirect_uri"]
                    or not hmac.compare_digest(_s256(verifier), row["code_challenge"])):
                raise OAuthError("invalid_grant", "authorization code, client, redirect_uri or verifier mismatch")
            if replayed:
                issued = None
            else:
                try:
                    issued = CapabilityStore(self.repo).create(
                        row["account_id"], f"oauth:{row['client_id']}",
                        {"label": row["client_name"], "scopes": json.loads(row["approved_scopes_json"]),
                         "expires_in_days": int(row["expires_in_days"])})
                except ValidationError as exc:  # e.g. too many active connections
                    raise OAuthError("invalid_grant", str(exc)) from None
                conn.execute("UPDATE oauth_authorizations SET status='EXCHANGED',grant_id=? WHERE id=?",
                             (issued["grant"]["id"], row["id"]))
        if issued is None:
            # A replayed code means it leaked: revoke what it already produced (committed
            # before the error is reported).
            if row["grant_id"]:
                CapabilityStore(self.repo).revoke(row["account_id"], row["grant_id"])
            raise OAuthError("invalid_grant", "authorization code was already used")
        expires = _dt(issued["grant"]["expires_at"])
        return {"access_token": issued["token"], "token_type": "Bearer",
                "expires_in": max(0, int((expires - now).total_seconds())) if expires else None,
                "scope": " ".join(issued["grant"]["scopes"])}


class RedirectError(OAuthError):
    """An authorization error that may be reported to the (validated) redirect URI."""

    def __init__(self, redirect_uri: str, error: str, description: str, state: str | None) -> None:
        super().__init__(error, description)
        self.redirect_uri = redirect_uri
        self.state = state

    def location(self, issuer: str) -> str:
        query = {"error": self.error, "error_description": str(self), "iss": issuer}
        if self.state is not None:
            query["state"] = self.state
        separator = "&" if urlsplit(self.redirect_uri).query else "?"
        return f"{self.redirect_uri}{separator}{urlencode(query)}"
