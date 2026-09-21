from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from student_execution_os.domain.errors import (
    AuthorizationDenied,
    DomainError,
    EntityNotFound,
    IdempotencyConflict,
    UnsupportedCapability,
    ValidationError,
    VersionConflict,
)

from .queries import UiService


_ERROR_MAP: tuple[tuple[type[Exception], str, int], ...] = (
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
                        "retryable": False,
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


def create_app(
    database: str | Path,
    *,
    account_id: str,
    principal_id: str,
    client_id: str = "web-ui",
    now=None,
) -> FastAPI:
    service = UiService(
        database,
        account_id=account_id,
        principal_id=principal_id,
        client_id=client_id,
        now=now,
    )
    app = FastAPI(
        title="Student Execution OS",
        version="1",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.ui_service = service

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
        diagnostics = service.diagnostics()
        return {
            "status": "ok",
            "service": diagnostics["service"],
            "version": diagnostics["version"],
            "schema_version": diagnostics["schema_version"],
        }

    @app.get("/api/v1/today")
    def today() -> dict[str, Any]:
        return service.today()

    @app.get("/api/v1/plan/current")
    def current_plan() -> dict[str, Any]:
        return service.plan()

    @app.get("/api/v1/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return service.tasks()

    @app.post("/api/v1/tasks", status_code=201)
    def create_task(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.create_task(payload)

    @app.patch("/api/v1/tasks/{task_id}")
    def update_task(task_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.update_task(task_id, payload)

    @app.post("/api/v1/obligations/{obligation_id}/{action}")
    def lifecycle(obligation_id: str, action: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.lifecycle(obligation_id, action, int(payload["expected_version"]))

    @app.get("/api/v1/events")
    def list_events() -> list[dict[str, Any]]:
        return service.events()

    @app.get("/api/v1/calendar")
    def calendar() -> dict[str, Any]:
        return service.calendar()

    @app.post("/api/v1/recurrence/templates", status_code=201)
    def create_recurring_template(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.create_recurring_template(payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/occurrences/{original_recurrence_id}/override")
    def override_recurring_occurrence(
        template_id: str, original_recurrence_id: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return service.override_recurring_occurrence(template_id, original_recurrence_id, payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/split")
    def split_recurring_series(template_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.split_recurring_series(template_id, payload)

    @app.get("/api/v1/notifications")
    def notifications() -> list[dict[str, Any]]:
        return service.notifications()

    @app.post("/api/v1/notifications/{notification_id}/snooze")
    def snooze_notification(notification_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.snooze_notification(notification_id, payload)

    @app.post("/api/v1/events", status_code=201)
    def create_event(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.create_event(payload)

    @app.patch("/api/v1/events/{event_id}")
    def update_event(event_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.update_event(event_id, payload)

    @app.get("/api/v1/evidence")
    def evidence() -> dict[str, Any]:
        return service.evidence()

    @app.get("/api/v1/places")
    def places() -> dict[str, Any]:
        return service.places()

    @app.get("/api/v1/settings/diagnostics")
    def diagnostics() -> dict[str, Any]:
        return service.diagnostics()

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
    def agent_cancel_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.agent_cancel_preview(
            str(payload["obligation_id"]),
            int(payload["expected_version"]),
        )

    @app.post("/api/v1/agent/cancel/confirm-execute")
    def agent_cancel_confirm_execute(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
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
