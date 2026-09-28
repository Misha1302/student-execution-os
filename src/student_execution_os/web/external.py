"""External-agent surface: capability-scoped REST and MCP over the canonical owners.

``CapabilityGateway`` is the only place a grant is turned into work. Reads call the
same ``UiService`` queries as the app (with fields outside the grant's scopes removed);
mutations are typed canonical operations applied by ``UiService.sync`` with actor
``USER_VIA_LLM`` and principal ``grant:<id>``, i.e. the ``/api/v1/sync`` path with op_id
exactly-once semantics, expected-version conflicts and account isolation.

``handle_mcp`` implements the MCP Streamable HTTP transport in stateless JSON mode
(JSON-RPC 2.0 over POST; no server-initiated stream).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from student_execution_os import __version__
from student_execution_os.capabilities import (
    DESTRUCTIVE_OPERATIONS,
    OPERATION_SCOPES,
    CapabilityDenied,
    Grant,
)
from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory

from .queries import UiService

MAX_OPERATIONS = 50
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_OP_NAMESPACE = uuid.UUID("5d2b4f4e-7a51-4f0b-9c3e-0a8d3c1c6b21")


class CapabilityGateway:
    def __init__(self, database: str, grant: Grant, *, now: Callable[[], datetime] | None = None) -> None:
        self.grant = grant
        self.service = UiService(database, account_id=grant.account_id, principal_id=grant.principal_id,
                                 client_id=f"capability:{grant.id}", now=now, binding="capability-bound")

    # ---- reads -------------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        allowed = sorted(op for op, scope in OPERATION_SCOPES.items() if scope in self.grant.scopes
                         and (op not in DESTRUCTIVE_OPERATIONS or "destructive" in self.grant.scopes))
        return {"grant": {"id": self.grant.id, "label": self.grant.label, "scopes": sorted(self.grant.scopes),
                          "expires_at": self.grant.expires_at},
                "allowed_operations": allowed}

    def today(self) -> dict[str, Any]:
        self.grant.require("today:read")
        today = self.service.today()
        # Today is a composite; parts owned by other scopes are withheld, not leaked.
        if "notes:read" not in self.grant.scopes:
            today["inbox_notes"] = []
        today["withheld"] = [] if "notes:read" in self.grant.scopes else ["inbox_notes"]
        return today

    def tasks(self) -> list[dict[str, Any]]:
        self.grant.require("tasks:read")
        return self.service.tasks()

    def calendar(self, range_name: str = "week", anchor: str | None = None) -> dict[str, Any]:
        self.grant.require("calendar:read")
        return self.service.outlook(range_name, anchor)

    def events(self) -> list[dict[str, Any]]:
        self.grant.require("calendar:read")
        return self.service.events()

    def notes(self, query: str = "", include_archived: bool = False) -> list[dict[str, Any]]:
        self.grant.require("notes:read")
        return self.service.notes(str(query or "")[:200], bool(include_archived))

    def note(self, note_id: str) -> dict[str, Any]:
        self.grant.require("notes:read")
        return self.service.note(str(note_id))

    def reminders(self) -> list[dict[str, Any]]:
        self.grant.require("reminders:read")
        return self.service.reminders()

    # ---- mutations ---------------------------------------------------------------

    def apply(self, operations: Any) -> dict[str, Any]:
        if not isinstance(operations, list) or not operations:
            raise ValidationError("operations must be a non-empty list")
        if len(operations) > MAX_OPERATIONS:
            raise ValidationError(f"at most {MAX_OPERATIONS} operations per call")
        # Authorize the whole batch before applying any of it.
        for op in operations:
            if not isinstance(op, dict):
                raise ValidationError("each operation must be an object")
            self.grant.require_operation(str(op.get("type") or ""))
        return self.service.sync({"operations": operations}, actor=ActorCategory.USER_VIA_LLM)

    def create(self, op_type: str, op_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Convenience create: the entity id is derived from op_id, so a retry is the same entity."""
        entity_id = uuid.uuid5(_OP_NAMESPACE, f"{self.grant.account_id}:{op_type}:{op_id}").hex
        return self.apply([{"op_id": op_id, "type": op_type, "entity_id": entity_id, "payload": payload}])


# ---- MCP -------------------------------------------------------------------------

_OP_ID = {"type": "string", "minLength": 8, "maxLength": 128, "pattern": "^[A-Za-z0-9_.:-]+$",
          "description": "Client-generated unique id. Retrying with the same op_id is exactly-once."}

TOOLS: list[dict[str, Any]] = [
    {"name": "get_capabilities", "scope": None,
     "description": "What this connection may do: its scopes and the canonical operation types it may run.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_today", "scope": "today:read",
     "description": "Today's plan, due tasks, classes/events and current action (notes only with notes:read).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_tasks", "scope": "tasks:read", "description": "All tasks with state and version.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_calendar", "scope": "calendar:read",
     "description": "Week or month outlook: events, classes (with personal changes) and due tasks.",
     "inputSchema": {"type": "object", "properties": {
         "range": {"type": "string", "enum": ["week", "month"], "default": "week"},
         "anchor": {"type": "string", "description": "ISO date inside the range; default today"}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_events", "scope": "calendar:read", "description": "Personal events with version.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_notes", "scope": "notes:read", "description": "Notes, optionally filtered by text.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 200}, "include_archived": {"type": "boolean"}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_note", "scope": "notes:read", "description": "One note by id.",
     "inputSchema": {"type": "object", "required": ["note_id"],
                     "properties": {"note_id": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_reminders", "scope": "reminders:read", "description": "Reminders with state and version.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_task", "scope": "tasks:write",
     "description": "Create a task (canonical task.create). Retrying with the same op_id never duplicates.",
     "inputSchema": {"type": "object", "required": ["op_id", "title"], "properties": {
         "op_id": _OP_ID, "title": {"type": "string", "minLength": 1, "maxLength": 200},
         "description": {"type": "string"},
         "estimated_total_effort_minutes": {"type": "integer", "minimum": 1},
         "actual_cutoff": {"type": "object", "description": '{"state":"KNOWN","at":"<ISO instant>"} or {"state":"ABSENT"}'},
         "importance": {"type": "string", "enum": ["LOW", "NORMAL", "HIGH", "CRITICAL"]}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "create_note", "scope": "notes:write",
     "description": "Create a note (canonical note.create). Retrying with the same op_id never duplicates.",
     "inputSchema": {"type": "object", "required": ["op_id", "content"], "properties": {
         "op_id": _OP_ID, "content": {"type": "string", "minLength": 1, "maxLength": 20000}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "apply_operations", "scope": None,
     "description": ("Apply typed canonical operations exactly as the botay! app does (/api/v1/sync): "
                     "{op_id, type, entity_id, payload}. Each result is APPLIED, NOOP, CONFLICT or REJECTED. "
                     "Field edits change only the fields sent; lifecycle changes that disagree with the "
                     "current state (e.g. starting a task cancelled elsewhere) return CONFLICT and are not "
                     "forced. *.delete also needs the destructive scope. Call get_capabilities for the "
                     "allowed types."),
     "inputSchema": {"type": "object", "required": ["operations"], "properties": {
         "operations": {"type": "array", "minItems": 1, "maxItems": MAX_OPERATIONS, "items": {
             "type": "object", "required": ["op_id", "type"], "properties": {
                 "op_id": _OP_ID, "type": {"type": "string", "enum": sorted(OPERATION_SCOPES)},
                 "entity_id": {"type": "string"}, "payload": {"type": "object"}}}}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}},
]
_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _call_tool(gateway: CapabilityGateway, name: str, args: dict[str, Any]) -> Any:
    if name == "get_capabilities":
        return gateway.capabilities()
    if name == "get_today":
        return gateway.today()
    if name == "list_tasks":
        return gateway.tasks()
    if name == "get_calendar":
        return gateway.calendar(str(args.get("range") or "week"), args.get("anchor"))
    if name == "list_events":
        return gateway.events()
    if name == "list_notes":
        return gateway.notes(args.get("query") or "", bool(args.get("include_archived", False)))
    if name == "get_note":
        return gateway.note(str(args.get("note_id") or ""))
    if name == "list_reminders":
        return gateway.reminders()
    if name == "create_task":
        payload = {key: args[key] for key in ("title", "description", "estimated_total_effort_minutes",
                                               "actual_cutoff", "importance") if key in args}
        return gateway.create("task.create", str(args.get("op_id") or ""), payload)
    if name == "create_note":
        return gateway.create("note.create", str(args.get("op_id") or ""), {"content": args.get("content"),
                                                                            "source_kind": "CAPTURE"})
    if name == "apply_operations":
        return gateway.apply(args.get("operations"))
    raise KeyError(name)


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if isinstance(value, dict):
        result["structuredContent"] = value
    else:
        result["structuredContent"] = {"items": value}
    return result


def handle_mcp(message: Any, gateway_factory: Callable[[], CapabilityGateway]) -> dict[str, Any] | None:
    """One JSON-RPC message -> response dict, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    method, request_id = message["method"], message.get("id")
    if "id" not in message:
        return None  # notifications/initialized, notifications/cancelled, ...
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "params must be an object")
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
        grant = gateway_factory().grant
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "botay", "title": "botay!", "version": __version__},
            "instructions": ("botay! is the student's planner. Reads reflect canonical state. Every change is a "
                             "typed operation with a client op_id (retry-safe: the same op_id is applied once). "
                             "A CONFLICT result means the item changed elsewhere; re-read and ask the user, "
                             "never force. Deleting needs the destructive scope. "
                             f"This connection's scopes: {', '.join(sorted(grant.scopes))}."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        grant = gateway_factory().grant
        visible = [tool for tool in TOOLS if tool["scope"] is None or tool["scope"] in grant.scopes]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {key: value for key, value in tool.items() if key != "scope"} for tool in visible]}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in _TOOLS_BY_NAME:
            return _rpc_error(request_id, -32602, f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return _rpc_error(request_id, -32602, "arguments must be an object")
        try:
            value = _call_tool(gateway_factory(), name, args)
        except CapabilityDenied as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": _tool_result({"error": "CAPABILITY_DENIED", "message": str(exc),
                                            "required_scope": exc.scope}, error=True)}
        except EntityNotFound as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": _tool_result({"error": "NOT_FOUND", "message": str(exc)}, error=True)}
        except (DomainError, ValueError) as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": _tool_result({"error": "VALIDATION_ERROR", "message": str(exc)}, error=True)}
        return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(value)}
    return _rpc_error(request_id, -32601, f"method {method!r} not found")
