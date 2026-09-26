from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import logging
import os
import time
import mimetypes
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

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from student_execution_os.groups.errors import GroupRateLimited

from .auth import AuthConfig, RateLimited, Session, SQLiteAuthStore, Unauthenticated
from .queries import GroupMutationRefused, UiService


_ERROR_MAP: tuple[tuple[type[Exception], str, int], ...] = (
    (Unauthenticated, "UNAUTHENTICATED", 401),
    (RateLimited, "RATE_LIMITED", 429),
    (GroupRateLimited, "RATE_LIMITED", 429),
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
    if isinstance(exc, GroupMutationRefused):
        return JSONResponse(status_code=exc.http_status, content={"error": {
            "code": exc.code, "message": str(exc), "retryable": False, **exc.details()}})
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


def _mutation_id(request: Request, payload: dict[str, Any]) -> str | None:
    return request.headers.get("idempotency-key") or payload.get("mutation_id")


def _install_group_routes(app: FastAPI, current_service) -> None:
    """Collaborative groups (schema v18, ADR 0021). Group-wide changes and personal
    overlay changes are separate routes and separate operation types."""
    from student_execution_os.groups.model import SharedKind

    def mutate(service: UiService, request: Request, op: str, entity_id: str, payload: dict[str, Any], scope=None):
        return service.group_mutation(op, entity_id, payload, _mutation_id(request, payload),
                                      if_match=request.headers.get("if-match"), scope=scope)

    @app.get("/api/v1/groups")
    async def list_groups(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return {"items": service.group_read("me_shared")["groups"], "next_cursor": None}

    @app.post("/api/v1/groups", status_code=201)
    async def create_group(request: Request, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)):
        body = dict(payload)
        return mutate(service, request, "group.create", str(body.pop("id", "") or ""), body)

    @app.post("/api/v1/groups/join")
    async def join_group(request: Request, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)):
        return mutate(service, request, "group.join", "", payload)

    @app.get("/api/v1/groups/{group_id}")
    async def get_group(group_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("group", group_id)

    @app.patch("/api/v1/groups/{group_id}")
    async def update_group(group_id: str, request: Request, payload: dict[str, Any] = Body(...),
                           service: UiService = Depends(current_service)):
        return mutate(service, request, "group.update", group_id, payload)

    for action in ("archive", "unarchive", "delete", "leave", "join"):
        def make(action=action):
            async def route(group_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                            service: UiService = Depends(current_service)):
                return mutate(service, request, f"group.{action}", "" if action == "join" else group_id, payload)
            return route
        app.post(f"/api/v1/groups/{{group_id}}/{action}", name=f"group_{action}")(make())

    @app.get("/api/v1/groups/{group_id}/members")
    async def group_members(group_id: str, include: str = "active", cursor: str | None = None, limit: int | None = None,
                            service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("members", group_id, include=include, cursor=cursor, limit=limit)

    @app.patch("/api/v1/groups/{group_id}/members/{account_id}")
    async def update_member(group_id: str, account_id: str, request: Request, payload: dict[str, Any] = Body(...),
                            service: UiService = Depends(current_service)):
        return mutate(service, request, "membership.update", group_id, {**payload, "account_id": account_id})

    @app.get("/api/v1/groups/{group_id}/invites")
    async def group_invites(group_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("invites", group_id)

    @app.post("/api/v1/groups/{group_id}/invites", status_code=201)
    async def create_invite(group_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                            service: UiService = Depends(current_service)):
        return mutate(service, request, "invite.create", group_id, payload)

    @app.post("/api/v1/groups/{group_id}/invites/{invite_id}/revoke")
    async def revoke_invite(group_id: str, invite_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                            service: UiService = Depends(current_service)):
        return mutate(service, request, "invite.revoke", invite_id, payload, scope=("INVITE", group_id))

    @app.get("/api/v1/groups/{group_id}/audit")
    async def group_audit(group_id: str, entity_id: str | None = None,
                          service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("audit", group_id, entity_id)

    shared = (("events", SharedKind.SHARED_EVENT, "shared_event", "cancel"),
              ("obligations", SharedKind.SHARED_OBLIGATION, "shared_obligation", "cancel"),
              ("announcements", SharedKind.SHARED_ANNOUNCEMENT, "announcement", "retract"))
    for path, kind, op, close in shared:
        def install(path=path, kind=kind, op=op, close=close):
            @app.get(f"/api/v1/groups/{{group_id}}/{path}", name=f"list_{path}")
            async def list_entities(group_id: str, status: str = "all", cursor: str | None = None, limit: int | None = None,
                                    service: UiService = Depends(current_service)) -> dict[str, Any]:
                return service.group_read("entities", kind, group_id, status=status, cursor=cursor, limit=limit)

            @app.post(f"/api/v1/groups/{{group_id}}/{path}", status_code=201, name=f"create_{path}")
            async def create_entity(group_id: str, request: Request, payload: dict[str, Any] = Body(...),
                                    service: UiService = Depends(current_service)):
                body = dict(payload)
                entity_id = str(body.pop("id", "") or "")
                return mutate(service, request, f"{op}.create", entity_id, {**body, "group_id": group_id})

            @app.get(f"/api/v1/groups/{{group_id}}/{path}/{{entity_id}}", name=f"get_{path}")
            async def get_entity(group_id: str, entity_id: str, service: UiService = Depends(current_service)):
                result = service.group_read("entity", kind, entity_id)
                if result["group_id"] != group_id:
                    raise EntityNotFound("not found")
                return result

            @app.patch(f"/api/v1/groups/{{group_id}}/{path}/{{entity_id}}", name=f"update_{path}")
            async def update_entity(group_id: str, entity_id: str, request: Request, payload: dict[str, Any] = Body(...),
                                    service: UiService = Depends(current_service)):
                return mutate(service, request, f"{op}.update", entity_id, payload, scope=(kind.value, group_id))

            @app.post(f"/api/v1/groups/{{group_id}}/{path}/{{entity_id}}/{close}", name=f"{close}_{path}")
            async def close_entity(group_id: str, entity_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                                   service: UiService = Depends(current_service)):
                return mutate(service, request, f"{op}.{close}", entity_id, payload, scope=(kind.value, group_id))
        install()

    @app.post("/api/v1/groups/{group_id}/events/{event_id}/external-binding")
    async def bind_external(group_id: str, event_id: str, request: Request, payload: dict[str, Any] = Body(...),
                            service: UiService = Depends(current_service)):
        return mutate(service, request, "shared_event.bind_external", event_id, payload,
                      scope=(SharedKind.SHARED_EVENT.value, group_id))

    @app.delete("/api/v1/groups/{group_id}/events/{event_id}/external-binding")
    async def detach_external(group_id: str, event_id: str, request: Request, expected_version: int | None = None,
                              mutation_id: str | None = None, service: UiService = Depends(current_service)):
        payload = {} if expected_version is None else {"expected_version": expected_version}
        if mutation_id:
            payload["mutation_id"] = mutation_id
        return mutate(service, request, "shared_event.detach_external", event_id, payload,
                      scope=(SharedKind.SHARED_EVENT.value, group_id))

    @app.get("/api/v1/groups/{group_id}/proposals")
    async def list_proposals(group_id: str, status: str = "pending", cursor: str | None = None, limit: int | None = None,
                             service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("proposals", group_id, status=status, cursor=cursor, limit=limit)

    @app.post("/api/v1/groups/{group_id}/proposals", status_code=201)
    async def create_proposal(group_id: str, request: Request, payload: dict[str, Any] = Body(...),
                              service: UiService = Depends(current_service)):
        body = dict(payload)
        entity_id = str(body.pop("id", "") or "")
        return mutate(service, request, "proposal.create", entity_id, {**body, "group_id": group_id})

    for action in ("approve", "reject", "withdraw"):
        def make_review(action=action):
            async def route(group_id: str, proposal_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                            service: UiService = Depends(current_service)):
                return mutate(service, request, f"proposal.{action}", proposal_id, payload, scope=("PROPOSAL", group_id))
            return route
        app.post(f"/api/v1/groups/{{group_id}}/proposals/{{proposal_id}}/{action}", name=f"proposal_{action}")(make_review())

    # ---- personal overlay: only the caller's own, never through a group route ----
    @app.get("/api/v1/me/shared")
    async def me_shared(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("me_shared")

    for path, kind, op in (("shared-events", SharedKind.SHARED_EVENT, "shared_event_state"),
                           ("shared-obligations", SharedKind.SHARED_OBLIGATION, "shared_obligation_state"),
                           ("announcements", SharedKind.SHARED_ANNOUNCEMENT, "announcement_state")):
        def install_state(path=path, kind=kind, op=op):
            @app.get(f"/api/v1/me/{path}/{{entity_id}}/state", name=f"me_{op}")
            async def get_state(entity_id: str, service: UiService = Depends(current_service)):
                return service.group_read("me_state", kind, entity_id)

            @app.patch(f"/api/v1/me/{path}/{{entity_id}}/state", name=f"me_{op}_update")
            async def update_state(entity_id: str, request: Request, payload: dict[str, Any] = Body(...),
                                   service: UiService = Depends(current_service)):
                return mutate(service, request, f"{op}.update", entity_id, payload)
        install_state()

    @app.get("/api/v1/me/groups/{group_id}/preferences")
    async def my_group_preferences(group_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.group_read("me_preferences", group_id)

    @app.patch("/api/v1/me/groups/{group_id}/preferences")
    async def update_my_group_preferences(group_id: str, request: Request, payload: dict[str, Any] = Body(...),
                                          service: UiService = Depends(current_service)):
        return mutate(service, request, "group_preferences.update", group_id, payload)


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

    @app.get("/api/v1/plan/agenda")
    async def plan_agenda(days: int = 7, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.agenda(days)

    @app.get("/api/v1/outlook")
    async def outlook(range: str = "week", anchor: str | None = None, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.outlook(range, anchor)

    @app.get("/api/v1/settings/planning-profile")
    async def planning_profile(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.planning_profile()

    @app.patch("/api/v1/settings/planning-profile")
    async def update_planning_profile(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.update_planning_profile(payload)

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

    @app.get("/api/v1/calendar")
    async def calendar(service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.calendar()

    @app.post("/api/v1/recurrence/templates", status_code=201)
    async def create_recurring_template(payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.create_recurring_template(payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/occurrences/{original_recurrence_id}/override")
    async def override_recurring_occurrence(
        template_id: str, original_recurrence_id: str, payload: dict[str, Any] = Body(...),
        service: UiService = Depends(current_service),
    ) -> dict[str, Any]:
        return service.override_recurring_occurrence(template_id, original_recurrence_id, payload)

    @app.post("/api/v1/recurrence/templates/{template_id}/split")
    async def split_recurring_series(template_id: str, payload: dict[str, Any] = Body(...), service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.split_recurring_series(template_id, payload)

    @app.get("/api/v1/commitments")
    async def list_commitments(place: str | None = None, q: str = "", service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.commitments(place, q)

    @app.get("/api/v1/connectors")
    async def list_connectors(service: UiService = Depends(current_service)) -> list[dict[str, Any]]:
        return service.connectors()

    @app.post("/api/v1/connectors/{connector_id}/sync")
    async def sync_connector(connector_id: str, service: UiService = Depends(current_service)) -> dict[str, Any]:
        return service.sync_connector(connector_id)

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
            headers={"Content-Disposition": 'attachment; filename="student-execution-os-export.json"'},
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

    _install_group_routes(app, current_service)

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
