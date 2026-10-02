from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import logging
import os
import time
import mimetypes
from urllib.parse import parse_qsl, unquote
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from student_execution_os import __version__
from student_execution_os.domain.errors import (
    AuthorizationDenied,
    DomainError,
    EntityNotFound,
    IdempotencyConflict,
    UnsupportedCapability,
    ValidationError,
    VersionConflict,
)

from student_execution_os.domain.clock import Clock, FrozenClock, SystemClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.agent.usage import StarterUsageStore
from student_execution_os.academic.model import AcademicProviderError
from student_execution_os.academic.ical import MAX_ICS_BYTES

from student_execution_os.capabilities import SCOPES, CapabilityDenied, CapabilityStore, InvalidGrant
from student_execution_os.groups import GroupService
from student_execution_os.oauth import (
    OAuthError,
    OAuthServer,
    RedirectError,
    authorization_server_metadata,
    protected_resource_metadata,
)

from .auth import AuthConfig, RateLimited, Session, SQLiteAuthStore, Unauthenticated
from .external import CapabilityGateway, handle_mcp
from .queries import UiService, _AccountLimiter

# Dynamic client registration is unauthenticated by design (RFC 7591); bound it per IP.
REGISTRATION_LIMITER = _AccountLimiter(10)
# Starting an authorization is unauthenticated too (it only stores a 10-minute request).
AUTHORIZE_LIMITER = _AccountLimiter(30)


_ERROR_MAP: tuple[tuple[type[Exception], str, int], ...] = (
    (Unauthenticated, "UNAUTHENTICATED", 401),
    (InvalidGrant, "INVALID_GRANT", 401),
    (CapabilityDenied, "CAPABILITY_DENIED", 403),
    (RateLimited, "RATE_LIMITED", 429),
    (EntityNotFound, "NOT_FOUND", 404),
    (VersionConflict, "VERSION_CONFLICT", 409),
    (IdempotencyConflict, "IDEMPOTENCY_CONFLICT", 409),
    (AuthorizationDenied, "AUTHORIZATION_DENIED", 403),
    (UnsupportedCapability, "UNSUPPORTED_CAPABILITY", 422),
    (ValidationError, "VALIDATION_ERROR", 422),
    (ValueError, "VALIDATION_ERROR", 422),
    (KeyError, "INVALID_REQUEST", 422),
)


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, AcademicProviderError):
        retryable = exc.code in {"NETWORK", "RATE_LIMITED", "PROVIDER_UNAVAILABLE", "CONCURRENT_SYNC_CONFLICT"}
        status = 503 if retryable else 422
        return JSONResponse(status_code=status, content={"error": {
            "code": exc.code, "message": str(exc), "retryable": retryable,
        }})
    for cls, code, status in _ERROR_MAP:
        if isinstance(exc, cls):
            return JSONResponse(
                status_code=status,
                content={
                    "error": {
                        "code": code,
                        "message": str(exc),
                        "retryable": status == 429,
                    }
                },
                headers={"WWW-Authenticate": (
                    f'Bearer realm="botay-capabilities", resource_metadata="{exc.resource_metadata}"'
                    if getattr(exc, "resource_metadata", None) else 'Bearer realm="botay-capabilities"')}
                if isinstance(exc, InvalidGrant) else None,
            )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unexpected server error",
                "retryable": False,
            }
        },
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def create_app(
    database: str | Path,
    *,
    account_id: str | None = None,
    principal_id: str | None = None,
    auth: AuthConfig | None = None,
    client_id: str = "web-ui",
    now=None,
) -> FastAPI:
    """Build the HTTP host.

    Bound mode (``account_id`` given) serves one operator-chosen account and is meant
    for loopback use. Session mode (``auth`` given) resolves the account from a bearer
    session token on every request; the client never supplies an account id.
    """
    if (account_id is None) == (auth is None):
        raise ValueError("exactly one of account_id (bound mode) or auth (session mode) is required")
    # Migration and STARTER backfill are startup work. INSERT OR IGNORE preserves any
    # operator/billing entitlement already attached to an existing account.
    with SQLiteCanonicalRepository(database) as startup_repo:
        startup_repo.initialize()
        StarterUsageStore(startup_repo).backfill_entitlements()
    auth_store = None if auth is None else SQLiteAuthStore(database, config=auth, now=now)

    def _clock() -> Clock:
        return FrozenClock(now()) if now is not None else SystemClock()
    bound_service = None
    if account_id is not None:
        bound_service = UiService(
            database,
            account_id=account_id,
            principal_id=principal_id or "local-user",
            client_id=client_id,
            now=now,
        )

    async def current_session(request: Request) -> Session:
        if auth_store is None:
            raise Unauthenticated("this server is bound to one account and has no sessions")
        return auth_store.authenticate(_bearer(request))

    async def current_service(request: Request) -> UiService:
        if bound_service is not None:
            return bound_service
        session = await current_session(request)
        return UiService(
            database,
            account_id=session.account_id,
            principal_id=session.user_id,
            client_id=client_id,
            now=now,
            binding="session-bound",
        )

    app = FastAPI(
        title="Student Execution OS",
        version="1",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.ui_service = bound_service
    app.state.auth_store = auth_store
    if auth is not None and auth.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(auth.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        supplied = request.headers.get("x-correlation-id", "")
        correlation_id = supplied if 0 < len(supplied) <= 128 and supplied.isascii() else str(uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        logging.getLogger("student_execution_os.requests").info(json.dumps({
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "latency_ms": elapsed_ms,
            "metric": "auth_latency" if request.url.path.startswith("/api/v1/auth/") else "http_latency",
        }, sort_keys=True))
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(self)"  # dictation in capture
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError):
        return _error(exc)

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, exc: ValueError):
        return _error(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError):
        # FastAPI's default body echoes the submitted input back, which for the AI
        # settings form would include an API key. Report only where it failed.
        where = ", ".join(".".join(str(part) for part in error.get("loc", ())) for error in exc.errors())
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": f"invalid request: {where}"[:300], "retryable": False}})

    @app.exception_handler(KeyError)
    async def handle_key_error(_: Request, exc: KeyError):
        return _error(exc)

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        with SQLiteCanonicalRepository(database) as repo:
            repo.initialize()
            schema_version = repo.schema_version()
        return {
            "status": "ok",
            "service": "student-execution-os",
            "version": __version__,
            "schema_version": schema_version,
            "auth_mode": "session" if auth is not None else "bound",
            "registration_open": bool(auth and auth.registration_open),
            "api_version": 1,
            "sync_protocol": 1,
            "revision": os.getenv("SEOS_REVISION", "unknown"),
        }

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _issued(issued) -> dict[str, Any]:
        return {
            "token": issued.token,
            "expires_at": issued.session.expires_at.isoformat(),
            "user": {"login": issued.session.login, "account_id": issued.session.account_id},
        }

    def _require_auth_store():
        if auth_store is None:
            raise Unauthenticated("this server is bound to one account and has no sessions")
        return auth_store

    @app.post("/api/v1/auth/register", status_code=201)
    async def register(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        store = _require_auth_store()
        return _issued(store.register(
            str(payload.get("login", "")), str(payload.get("password", "")),
            client_ip=_client_ip(request), device_label=payload.get("device_label"),
        ))

    @app.post("/api/v1/auth/login")
    async def login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        store = _require_auth_store()
        return _issued(store.login(
            str(payload.get("login", "")), str(payload.get("password", "")),
            client_ip=_client_ip(request), device_label=payload.get("device_label"),
        ))

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request) -> dict[str, Any]:
        store = _require_auth_store()
        token = _bearer(request)
        if token:
            store.logout(token)
        return {"status": "logged_out"}

    @app.get("/api/v1/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        if bound_service is not None:
            return {"login": bound_service.principal.principal_id, "account_id": bound_service.account_id, "auth_mode": "bound"}
        session = await current_session(request)
        return {"login": session.login, "account_id": session.account_id, "auth_mode": "session"}

    @app.get("/api/v1/today")
    async def today(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.today()

    @app.get("/api/v1/plan/current")
    async def current_plan(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.plan()

    @app.get("/api/v1/execution/active")
    async def execution_active(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.execution_active()

    @app.get("/api/v1/execution/sessions")
    async def execution_sessions(task_id: str | None = None, days: int = 90, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.execution_sessions(task_id, days)

    @app.get("/api/v1/execution/sessions/{session_id}")
    async def execution_session(session_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.execution_session(session_id)

    @app.get("/api/v1/plan/agenda")
    async def plan_agenda(days: int = 7, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agenda(days)

    @app.get("/api/v1/plan/constraints")
    async def plan_constraints(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.plan_constraints()

    @app.post("/api/v1/plan/control/preview")
    async def plan_control_preview(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.plan_control_preview(payload)

    @app.get("/api/v1/outlook")
    async def outlook(range: str = "week", anchor: str | None = None, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.outlook(range, anchor)

    @app.get("/api/v1/settings/planning-profile")
    async def planning_profile(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.planning_profile()

    @app.patch("/api/v1/settings/planning-profile")
    async def update_planning_profile(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_planning_profile(payload)

    @app.get("/api/v1/projects")
    async def list_projects(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.projects()

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(project_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.project(project_id)

    @app.get("/api/v1/notes")
    async def list_notes(q: str = "", include_archived: bool = False, service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.notes(q, include_archived)

    @app.get("/api/v1/notes/{note_id}")
    async def get_note(note_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.note(note_id)

    @app.put("/api/v1/notes/{note_id}/audio", status_code=201)
    async def put_note_audio(note_id: str, request: Request, service: UiService = Depends(current_service)) -> dict[str, Any]:
        content = await request.body()
        return service.save_note_audio(
            note_id,
            request.headers.get("content-type", "application/octet-stream"),
            request.headers.get("x-filename"),
            content,
        )

    @app.get("/api/v1/notes/{note_id}/audio")
    async def get_note_audio(note_id: str, service: UiService = Depends(current_service)) -> Response:
        metadata, content = service.note_audio(note_id)
        return Response(
            content=content,
            media_type=metadata["mime_type"],
            headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/v1/feedback", status_code=201)
    async def beta_feedback(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.beta_feedback(payload)

    @app.get("/api/v1/tasks")
    async def list_tasks(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.tasks()

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_task(payload)

    @app.patch("/api/v1/tasks/{task_id}")
    async def update_task(task_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_task(task_id, payload)

    @app.post("/api/v1/tasks/{task_id}/activate")
    async def activate_task(task_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.activate_task(task_id, payload)

    @app.post("/api/v1/tasks/{task_id}/defer")
    async def defer_task(task_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.defer_task(task_id, payload)

    @app.post("/api/v1/tasks/{task_id}/start")
    async def start_task(task_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.start_task(task_id, payload)

    @app.post("/api/v1/sync")
    async def sync(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.sync(payload)

    @app.post("/api/v1/attachments", status_code=201)
    async def upload_attachment(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.upload_attachment(payload)

    @app.get("/api/v1/attachments")
    async def list_attachments(owner_kind: str, owner_id: str, service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.attachments(owner_kind, owner_id)

    @app.get("/api/v1/attachments/{attachment_id}/download")
    async def download_attachment(attachment_id: str, service: UiService = Depends(current_service)) -> Response:
        from urllib.parse import quote
        metadata, content = service.download_attachment(attachment_id)
        mime = metadata["mime_type"]
        if mime in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
            mime = "application/octet-stream"
        return Response(
            content=content, media_type=mime,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(metadata['original_name'])}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.post("/api/v1/attachment-links/{link_id}/unlink")
    async def unlink_attachment(link_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.unlink_attachment(link_id, payload)

    @app.get("/api/v1/tasks/saved-views")
    async def list_saved_views(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.saved_views()

    # Declared after /tasks/saved-views so that literal path keeps precedence.
    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.task(task_id)

    @app.post("/api/v1/tasks/saved-views", status_code=201)
    async def create_saved_view(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_saved_view(payload)

    @app.patch("/api/v1/tasks/saved-views/{view_id}")
    async def update_saved_view(view_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_saved_view(view_id, payload)

    @app.delete("/api/v1/tasks/saved-views/{view_id}")
    async def delete_saved_view(view_id: str, expected_version: int, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.delete_saved_view(view_id, expected_version)

    @app.post("/api/v1/obligations/{obligation_id}/{action}")
    async def lifecycle(obligation_id: str, action: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.lifecycle(obligation_id, action, int(payload["expected_version"]))

    @app.get("/api/v1/events")
    async def list_events(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.events()

    @app.get("/api/v1/work-routines")
    async def work_routines(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.work_routines()

    @app.get("/api/v1/reflection")
    async def reflection(days: int = 7, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.reflection(days)

    @app.get("/api/v1/reflection/daily")
    async def reflection_daily(local_date: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.reflection_daily(local_date)

    @app.get("/api/v1/calendar")
    async def calendar(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.calendar()

    @app.get("/api/v1/commitments")
    async def list_commitments(place: str | None = None, q: str = "", service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.commitments(place, q)

    @app.get("/api/v1/connectors")
    async def list_connectors(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.connectors()

    @app.post("/api/v1/connectors/{connector_id}/sync")
    async def sync_connector(connector_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.sync_connector(connector_id)

    @app.get("/api/v1/settings/academic-schedule")
    async def academic_schedule(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.academic_schedule()

    # Connect and refresh fetch a remote feed (bounded retries); plain ``def`` runs them in
    # the threadpool so a slow calendar provider never blocks the event loop.
    @app.put("/api/v1/settings/academic-schedule")
    def connect_academic_schedule(
        payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)
    ) -> dict[str, Any]:
        return service.connect_academic_schedule(payload)

    @app.post("/api/v1/settings/academic-schedule/import")
    async def import_academic_schedule(
        request: Request, service: UiService = Depends(current_service)
    ) -> dict[str, Any]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_ICS_BYTES:
                raise ValidationError("calendar file is too large")
            chunks.append(chunk)
        return service.import_academic_schedule(
            b"".join(chunks),
            # Header values are Latin-1 on the wire, so the client percent-encodes the file name.
            display_name=unquote(request.headers.get("x-calendar-name", "Imported academic calendar"))[:120],
            default_timezone=request.headers.get("x-calendar-timezone", "Europe/Moscow")[:80],
        )

    @app.post("/api/v1/settings/academic-schedule/sync")
    def refresh_academic_schedule(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.refresh_academic_schedule()

    @app.delete("/api/v1/settings/academic-schedule")
    async def disconnect_academic_schedule(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.disconnect_academic_schedule()

    @app.get("/api/v1/reminders")
    async def list_reminders(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.reminders()

    @app.get("/api/v1/reminders/alarms")
    async def upcoming_alarms(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.upcoming_alarms()

    @app.get("/api/v1/notifications")
    async def notifications(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.notifications()

    @app.get("/api/v1/notification-preferences")
    async def notification_preferences(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.notification_preferences()

    @app.patch("/api/v1/notification-preferences")
    async def update_notification_preferences(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_notification_preferences(payload)

    @app.get("/api/v1/mobile/devices")
    async def list_mobile_devices(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.devices()

    @app.post("/api/v1/mobile/devices", status_code=201)
    async def register_mobile_device(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.register_device(payload)

    @app.post("/api/v1/mobile/devices/{device_id}/status")
    async def report_mobile_device_status(device_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.report_device_status(device_id, payload)

    @app.get("/api/v1/notifications/health")
    async def notifications_health(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.notifications_health()

    @app.post("/api/v1/notifications/test", status_code=201)
    async def notifications_test(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.send_test_notification()

    @app.get("/api/v1/notifications/{notification_id}/delivery")
    async def notification_delivery(notification_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.notification_delivery(notification_id)

    @app.post("/api/v1/mobile/devices/{device_id}/revoke")
    async def revoke_mobile_device(device_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.revoke_device(device_id, payload)

    @app.post("/api/v1/notifications/{notification_id}/snooze")
    async def snooze_notification(notification_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.snooze_notification(notification_id, payload)

    @app.post("/api/v1/events", status_code=201)
    async def create_event(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_event(payload)

    @app.patch("/api/v1/events/{event_id}")
    async def update_event(event_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_event(event_id, payload)

    @app.post("/api/v1/events/{event_id}/location-selection")
    async def select_event_location(event_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.select_event_location(event_id, payload)

    @app.get("/api/v1/evidence")
    async def evidence(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.evidence()

    @app.get("/api/v1/places")
    async def places(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.places()

    @app.get("/api/v1/settings/diagnostics")
    async def diagnostics(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.diagnostics()

    @app.get("/api/v1/account/export")
    async def account_export(service: UiService = Depends(current_service)) -> JSONResponse:
        return JSONResponse(
            content=service.account_export(),
            headers={"Content-Disposition": 'attachment; filename="botay-export.json"'},
        )

    @app.get("/api/v1/account/deletion-policy")
    async def account_deletion_policy(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.account_deletion_policy()

    @app.post("/api/v1/account/delete")
    async def account_delete(
        request: Request, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)
    ) -> dict[str, Any]:
        if auth_store is not None and "confirm_login" in payload:
            session = await current_session(request)
            if str(payload["confirm_login"]).strip().lower() == session.login:
                payload = {**payload, "confirm_account_id": session.account_id}
        return service.delete_account(payload)

    @app.get("/api/v1/ask/capabilities")
    async def ask_capabilities(service: UiService = Depends(current_service)) -> dict[str, Any]:
        result = service.assistant_capabilities()
        return {
            **result,
            "explanations": True,
            "destructive_action_preview": True,
            "message": "Live structured Assistant is available." if result["live_llm_provider"] else (
                "No AI key is set up for this account. Deterministic task/event capture remains available."
            ),
        }

    # ---- Collaborative groups (shared academic facts; personal state stays personal) --
    def _groups(service: UiService, action):
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return action(GroupService(repo, account_id=service.account_id))

    @app.get("/api/v1/groups")
    def list_groups(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return _groups(service, lambda groups: groups.list())

    @app.post("/api/v1/groups", status_code=201)
    def create_group(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.create(payload))

    @app.post("/api/v1/groups/join")
    def join_group(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.join(payload))

    @app.get("/api/v1/groups/{group_id}")
    def group_detail(group_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return _groups(service, lambda groups: groups.detail(group_id))

    @app.post("/api/v1/groups/{group_id}/invitations", status_code=201)
    def group_invite(group_id: str, payload: dict[str, Any] = Body(...),
                     service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.invite(group_id, payload))

    @app.delete("/api/v1/groups/{group_id}/invitations/{invitation_id}")
    def group_revoke_invite(group_id: str, invitation_id: str, service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.revoke_invitation(group_id, invitation_id))

    @app.post("/api/v1/groups/{group_id}/leave")
    def group_leave(group_id: str, service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.leave(group_id))

    @app.delete("/api/v1/groups/{group_id}/members/{member_id}")
    def group_remove_member(group_id: str, member_id: str, service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.remove_member(group_id, member_id))

    @app.post("/api/v1/groups/{group_id}/members/{member_id}/role")
    def group_set_role(group_id: str, member_id: str, payload: dict[str, Any] = Body(...),
                       service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.set_role(group_id, member_id, payload))

    @app.put("/api/v1/groups/{group_id}/schedule/{uid}")
    def group_publish(group_id: str, uid: str, payload: dict[str, Any] = Body(...),
                      service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.publish(group_id, uid, payload))

    @app.post("/api/v1/groups/{group_id}/schedule/{uid}/remove")
    def group_unpublish(group_id: str, uid: str, payload: dict[str, Any] = Body(...),
                        service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.unpublish(group_id, uid, payload))

    @app.post("/api/v1/groups/{group_id}/proposals", status_code=201)
    def group_propose(group_id: str, payload: dict[str, Any] = Body(...),
                      service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.propose(group_id, payload))

    @app.post("/api/v1/groups/{group_id}/proposals/{proposal_id}/decide")
    def group_decide(group_id: str, proposal_id: str, payload: dict[str, Any] = Body(...),
                     service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.decide(group_id, proposal_id, payload))

    @app.post("/api/v1/groups/{group_id}/proposals/{proposal_id}/withdraw")
    def group_withdraw(group_id: str, proposal_id: str, service: UiService = Depends(current_service)):
        return _groups(service, lambda groups: groups.withdraw(group_id, proposal_id))

    # ---- External agents (MCP / ChatGPT / Codex): capability grants -----------------
    # Grants are managed only by the signed-in owner (session); a grant token cannot
    # manage grants. The plaintext token is returned once, at creation.
    @app.get("/api/v1/settings/capabilities")
    def list_capability_grants(service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database) as repo:
            repo.initialize()
            return {"grants": CapabilityStore(repo).list(service.account_id),
                    "scopes": [{"id": scope, "description": text} for scope, text in SCOPES.items()]}

    @app.post("/api/v1/settings/capabilities", status_code=201)
    def create_capability_grant(payload: dict[str, Any] = Body(...),
                                service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return CapabilityStore(repo).create(service.account_id, service.principal.principal_id, payload)

    @app.delete("/api/v1/settings/capabilities/{grant_id}")
    def revoke_capability_grant(grant_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return CapabilityStore(repo).revoke(service.account_id, grant_id)

    def current_gateway(request: Request) -> CapabilityGateway:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            try:
                grant = CapabilityStore(repo).authenticate(_bearer(request))
            except InvalidGrant as exc:
                # MCP authorization discovery (RFC 9728): tell the client where to start OAuth.
                exc.resource_metadata = f"{_origin(request)}/.well-known/oauth-protected-resource"
                raise
        return CapabilityGateway(str(database), grant, now=now)

    # ---- OAuth 2.1 for MCP clients (ChatGPT, Codex): consent issues a capability grant --
    def _origin(request: Request) -> str:
        configured = os.environ.get("SEOS_PUBLIC_ORIGIN", "").strip().rstrip("/")
        return configured or str(request.base_url).rstrip("/")

    def _oauth_error(exc: OAuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.error, "error_description": str(exc)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def oauth_protected_resource(request: Request) -> dict[str, Any]:
        return protected_resource_metadata(_origin(request))

    @app.get("/.well-known/oauth-authorization-server")
    def oauth_authorization_server(request: Request) -> dict[str, Any]:
        return authorization_server_metadata(_origin(request))

    @app.post("/oauth/register", status_code=201)
    def oauth_register(request: Request, payload: dict[str, Any] = Body(...)):
        if not REGISTRATION_LIMITER.allow(request.client.host if request.client else "unknown"):
            return JSONResponse(status_code=429, content={"error": "slow_down",
                                                          "error_description": "too many registrations"})
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            try:
                return OAuthServer(repo, issuer=_origin(request)).register(payload)
            except OAuthError as exc:
                return _oauth_error(exc)

    @app.get("/oauth/authorize")
    def oauth_authorize(request: Request):
        if not AUTHORIZE_LIMITER.allow(request.client.host if request.client else "unknown"):
            return JSONResponse(status_code=429, content={"error": "slow_down",
                                                          "error_description": "too many authorization requests"})
        params = dict(request.query_params)
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            server = OAuthServer(repo, issuer=_origin(request))
            try:
                request_id = server.start(params)
            except RedirectError as exc:
                return Response(status_code=302, headers={"Location": exc.location(server.issuer)})
            except OAuthError as exc:
                # Unknown client or unregistered redirect_uri: never redirect anywhere.
                return JSONResponse(status_code=400, content={"error": exc.error, "error_description": str(exc)})
        # Consent happens inside the signed-in app (sessions are bearer tokens there).
        return Response(status_code=302, headers={"Location": f"/#/connect/{request_id}"})

    @app.get("/api/v1/oauth/requests/{request_id}")
    def oauth_request(request_id: str, request: Request,
                      service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return OAuthServer(repo, issuer=_origin(request)).describe(request_id)

    @app.post("/api/v1/oauth/requests/{request_id}/approve")
    def oauth_approve(request_id: str, request: Request, payload: dict[str, Any] = Body(...),
                      service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return OAuthServer(repo, issuer=_origin(request)).approve(request_id, service.account_id, payload)

    @app.post("/api/v1/oauth/requests/{request_id}/deny")
    def oauth_deny(request_id: str, request: Request,
                   service: UiService = Depends(current_service)) -> dict[str, Any]:
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            return OAuthServer(repo, issuer=_origin(request)).deny(request_id, service.account_id)

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        raw = await request.body()
        if len(raw) > 8192:
            return _oauth_error(OAuthError("invalid_request", "request too large"))
        if request.headers.get("content-type", "").split(";")[0].strip() == "application/json":
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                parsed = None
            form = {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        else:
            form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
        with SQLiteCanonicalRepository(database, clock=_clock()) as repo:
            repo.initialize()
            try:
                return JSONResponse(content=OAuthServer(repo, issuer=_origin(request)).exchange(form),
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            except OAuthError as exc:
                return _oauth_error(exc)

    @app.get("/api/v1/ext/capabilities")
    def ext_capabilities(gateway: CapabilityGateway = Depends(current_gateway)) -> dict[str, Any]:
        return gateway.capabilities()

    @app.get("/api/v1/ext/today")
    def ext_today(gateway: CapabilityGateway = Depends(current_gateway)) -> dict[str, Any]:
        return gateway.today()

    @app.get("/api/v1/ext/tasks")
    def ext_tasks(gateway: CapabilityGateway = Depends(current_gateway)) -> list[dict[str, Any]]:
        return gateway.tasks()

    @app.get("/api/v1/ext/calendar")
    def ext_calendar(range: str = "week", anchor: str | None = None,
                     gateway: CapabilityGateway = Depends(current_gateway)) -> dict[str, Any]:
        return gateway.calendar(range, anchor)

    @app.get("/api/v1/ext/events")
    def ext_events(gateway: CapabilityGateway = Depends(current_gateway)) -> list[dict[str, Any]]:
        return gateway.events()

    @app.get("/api/v1/ext/notes")
    def ext_notes(q: str = "", include_archived: bool = False,
                  gateway: CapabilityGateway = Depends(current_gateway)) -> list[dict[str, Any]]:
        return gateway.notes(q, include_archived)

    @app.get("/api/v1/ext/notes/{note_id}")
    def ext_note(note_id: str, gateway: CapabilityGateway = Depends(current_gateway)) -> dict[str, Any]:
        return gateway.note(note_id)

    @app.get("/api/v1/ext/reminders")
    def ext_reminders(gateway: CapabilityGateway = Depends(current_gateway)) -> list[dict[str, Any]]:
        return gateway.reminders()

    @app.post("/api/v1/ext/operations")
    def ext_operations(payload: dict[str, Any] = Body(...),
                       gateway: CapabilityGateway = Depends(current_gateway)) -> dict[str, Any]:
        return gateway.apply(payload.get("operations"))

    @app.post("/mcp")
    def mcp(payload: Any = Body(...), gateway: CapabilityGateway = Depends(current_gateway)):
        if isinstance(payload, list):  # JSON-RPC batches are not part of MCP 2025-06-18
            return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "batch requests are not supported"}})
        response = handle_mcp(payload, lambda: gateway)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(content=response)

    @app.get("/mcp")
    def mcp_stream() -> Response:
        # Stateless JSON mode: no server-initiated SSE stream is offered.
        return Response(status_code=405, headers={"Allow": "POST"})

    # Per-account AI (LLM) credentials. Responses carry only a masked key hint.
    @app.get("/api/v1/settings/llm")
    async def llm_settings(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.llm_settings()

    @app.put("/api/v1/settings/llm")
    async def save_llm_settings(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.save_llm_settings(payload)

    @app.delete("/api/v1/settings/llm")
    async def delete_llm_settings(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.delete_llm_settings()

    @app.post("/api/v1/settings/llm/test")
    async def test_llm_settings(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.test_llm_settings()

    @app.post("/api/v1/assistant/interpret")
    async def assistant_interpret(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.assistant_interpret(payload)

    @app.post("/api/v1/assistant/apply")
    async def assistant_apply(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.assistant_apply(payload)

    @app.post("/api/v1/assistant/undo")
    async def assistant_undo(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.assistant_undo(payload)

    @app.post("/api/v1/agent/cancel/preview")
    async def agent_cancel_preview(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agent_cancel_preview(
            str(payload["obligation_id"]),
            int(payload["expected_version"]),
        )

    @app.post("/api/v1/agent/cancel/confirm-execute")
    async def agent_cancel_confirm_execute(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agent_cancel_confirm_execute(
            str(payload["intent_id"]),
            payload.get("idempotency_key"),
        )

    static_root = Path(__file__).with_name("static")
    @app.get("/assets/{asset_path:path}")
    async def asset(asset_path: str):
        target = (static_root / asset_path).resolve()
        if static_root.resolve() not in target.parents or not target.is_file():
            return Response(status_code=404)
        return Response(
            target.read_bytes(),
            media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            # Revalidate on every load: after a deploy the browser must not keep old
            # modules (e.g. translations without a status the new server reports).
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/")
    async def index():
        return Response((static_root / "index.html").read_bytes(), media_type="text/html")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "API route not found", "retryable": False}})
        return Response((static_root / "index.html").read_bytes(), media_type="text/html")

    return app
