from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from .auth import AuthConfig, RateLimited, Session, SQLiteAuthStore, Unauthenticated
from .queries import UiService


_ERROR_MAP: tuple[tuple[type[Exception], str, int], ...] = (
    (Unauthenticated, "UNAUTHENTICATED", 401),
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
    auth_store = None if auth is None else SQLiteAuthStore(database, config=auth, now=now)
    bound_service = None
    if account_id is not None:
        bound_service = UiService(
            database,
            account_id=account_id,
            principal_id=principal_id or "local-user",
            client_id=client_id,
            now=now,
        )

    def current_session(request: Request) -> Session:
        if auth_store is None:
            raise Unauthenticated("this server is bound to one account and has no sessions")
        return auth_store.authenticate(_bearer(request))

    def current_service(request: Request) -> UiService:
        if bound_service is not None:
            return bound_service
        session = current_session(request)
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
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
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

    @app.exception_handler(KeyError)
    async def handle_key_error(_: Request, exc: KeyError):
        return _error(exc)

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
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
    def register(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        store = _require_auth_store()
        return _issued(store.register(
            str(payload.get("login", "")), str(payload.get("password", "")),
            client_ip=_client_ip(request), device_label=payload.get("device_label"),
        ))

    @app.post("/api/v1/auth/login")
    def login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        store = _require_auth_store()
        return _issued(store.login(
            str(payload.get("login", "")), str(payload.get("password", "")),
            client_ip=_client_ip(request), device_label=payload.get("device_label"),
        ))

    @app.post("/api/v1/auth/logout")
    def logout(request: Request) -> dict[str, Any]:
        store = _require_auth_store()
        token = _bearer(request)
        if token:
            store.logout(token)
        return {"status": "logged_out"}

    @app.get("/api/v1/auth/me")
    def me(request: Request) -> dict[str, Any]:
        if bound_service is not None:
            return {"login": bound_service.principal.principal_id, "account_id": bound_service.account_id, "auth_mode": "bound"}
        session = current_session(request)
        return {"login": session.login, "account_id": session.account_id, "auth_mode": "session"}

    @app.get("/api/v1/today")
    def today(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.today()

    @app.get("/api/v1/plan/current")
    def current_plan(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.plan()

    @app.get("/api/v1/tasks")
    def list_tasks(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.tasks()

    @app.post("/api/v1/tasks", status_code=201)
    def create_task(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_task(payload)

    @app.patch("/api/v1/tasks/{task_id}")
    def update_task(task_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_task(task_id, payload)

    @app.post("/api/v1/obligations/{obligation_id}/{action}")
    def lifecycle(obligation_id: str, action: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.lifecycle(obligation_id, action, int(payload["expected_version"]))

    @app.get("/api/v1/events")
    def list_events(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.events()

    @app.get("/api/v1/calendar")
    def calendar(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.calendar()

    @app.post("/api/v1/recurrence/templates", status_code=201)
    def create_recurring_template(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_recurring_template(payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/occurrences/{original_recurrence_id}/override")
    def override_recurring_occurrence(
        template_id: str, original_recurrence_id: str, payload: dict[str, Any] = Body(...),
        service: UiService = Depends(current_service),
    ) -> dict[str, Any]:
        return service.override_recurring_occurrence(template_id, original_recurrence_id, payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/split")
    def split_recurring_series(template_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.split_recurring_series(template_id, payload)

    @app.get("/api/v1/notifications")
    def notifications(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.notifications()

    @app.post("/api/v1/notifications/{notification_id}/snooze")
    def snooze_notification(notification_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.snooze_notification(notification_id, payload)

    @app.post("/api/v1/events", status_code=201)
    def create_event(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_event(payload)

    @app.patch("/api/v1/events/{event_id}")
    def update_event(event_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_event(event_id, payload)

    @app.get("/api/v1/evidence")
    def evidence(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.evidence()

    @app.get("/api/v1/places")
    def places(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.places()

    @app.get("/api/v1/settings/diagnostics")
    def diagnostics(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.diagnostics()

    @app.get("/api/v1/account/export")
    def account_export(service: UiService = Depends(current_service)) -> JSONResponse:
        return JSONResponse(
            content=service.account_export(),
            headers={"Content-Disposition": 'attachment; filename="student-execution-os-export.json"'},
        )

    @app.get("/api/v1/account/deletion-policy")
    def account_deletion_policy(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.account_deletion_policy()

    @app.post("/api/v1/account/delete")
    def account_delete(
        request: Request, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)
    ) -> dict[str, Any]:
        if auth_store is not None and "confirm_login" in payload:
            session = current_session(request)
            if str(payload["confirm_login"]).strip().lower() == session.login:
                payload = {**payload, "confirm_account_id": session.account_id}
        return service.delete_account(payload)

    @app.get("/api/v1/ask/capabilities")
    def ask_capabilities() -> dict[str, Any]:
        return {
            "live_llm_provider": False,
            "explanations": True,
            "destructive_action_preview": True,
            "message": (
                "No live LLM provider is configured in this release. The UI exposes server-owned "
                "explanations and the authenticated action-intent boundary without fabricating a chat provider."
            ),
        }

    @app.post("/api/v1/agent/cancel/preview")
    def agent_cancel_preview(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agent_cancel_preview(
            str(payload["obligation_id"]),
            int(payload["expected_version"]),
        )

    @app.post("/api/v1/agent/cancel/confirm-execute")
    def agent_cancel_confirm_execute(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agent_cancel_confirm_execute(
            str(payload["intent_id"]),
            payload.get("idempotency_key"),
        )

    static_root = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_root), name="assets")

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/{path:path}")
    def spa_fallback(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "API route not found", "retryable": False}})
        return FileResponse(static_root / "index.html")

    return app
