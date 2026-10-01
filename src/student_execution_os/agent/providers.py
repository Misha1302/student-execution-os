"""Server-side LLM providers for the controlled Assistant preview boundary.

Providers may only return the small typed proposal schema consumed by
``SQLiteAssistantService``.  They never receive a repository, bearer token, or a
mutation tool, and therefore cannot apply their own output.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import socket
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from student_execution_os.domain.errors import ValidationError
from student_execution_os.netguard import (
    IpAddress,
    PinnedNetworkBackend,
    PinnedTransport,
    is_public_address,
    wire_host,
)

_log = logging.getLogger("student_execution_os.llm")


SYSTEM_PROMPT = """You interpret what a student wants to do for Student Execution OS. Return JSON only:
{"message":"short helpful response","actions":[{"client_ref":"optional-local-name","depends_on":["earlier-client-ref"],"command":"CREATE_TASK|CREATE_EVENT|CREATE_REMINDER|CREATE_NOTE|UPDATE_TASK|UPDATE_EVENT|UPDATE_REMINDER|RESCHEDULE|SNOOZE|LOG_PROGRESS|COMPLETE_OBLIGATION|CANCEL_OBLIGATION|ARCHIVE_OBLIGATION|REFINE_TASK|CREATE_TIME_CONSTRAINT|UNDO_LAST","payload":{},"confidence":0.0,"unresolved_fields":[],"expected_version":null,"requires_confirmation":false,"field_provenance":{"field":"MODEL_EXPLICIT|MODEL_INFERRED"}}],"read_query":null}
Never claim an action was executed; every action is only a proposal the user reviews.
Later explicit corrections replace earlier propositions, preserving unrelated facts.
When context.assistant_session is present, its previous_actions are the bounded prior
semantic turn. Resolve pronouns and corrections against it; do not create a new item
unless the latest user text explicitly asks for one. Fields marked USER_EDIT remain
unchanged unless the latest text explicitly corrects that same field.
For a factual question return actions=[] and one read_query. Never answer from general
knowledge and never invent planner reasons. Allowed read_query shapes are:
  {kind:"AGENDA_WINDOW",starts_at,ends_at}; {kind:"FREE_TIME",starts_at,ends_at};
  {kind:"ITEM_LOOKUP",obligation_id|reminder_id}; {kind:"DUE_BEFORE",before};
  {kind:"URGENT_TASKS"}; {kind:"WHAT_NOW"};
  {kind:"PLAN_EXPLANATION",obligation_id}.
The server executes these typed read queries against authorized canonical state. Do not
write SQL or include an identifier that is absent from context.
Planner-control language becomes canonical constraints, never plan blocks:
  CREATE_TIME_CONSTRAINT {type:"UNAVAILABLE"|"FIXED_PERSONAL_BLOCK",starts_at,ends_at,reason?}.
Use it for explicit protected/unavailable windows such as "завтра ничего до 12" or
"оставь этот час свободным". Do not use it for vague preferences that need a new
domain concept, and never claim that a derived plan block was edited.
UNDO_LAST payload is {}. Use it only for an explicit request to undo the most recent
Assistant-originated reversible mutation. The server checks the current entity version
before applying a stored inverse; never reconstruct stale values yourself.
For multiple actions, give each action a unique client_ref and list only earlier refs in
depends_on. The server replaces refs with opaque action ids, validates the whole graph,
and applies the selected dependency-closed plan atomically in declared order.
CREATE_NOTE payload: {content}: an idea, reference or unstructured note, not scheduled work.
Preserve meaningful newlines in notes. Example: "Идея для курсовой: расписание как граф".
CREATE_TASK payload (omit what the user did not say; no other keys are accepted):
  title: short clean title in the user's language ("Сдать лабораторную по физике"): no dates,
         times, durations or filler words; fix obvious speech-recognition slips
  description?: extra details the user gave
  estimated_total_effort_minutes?: whole minutes ("часа два" = 120, "полчаса" = 30)
  actual_cutoff?: the hard deadline: {"state":"KNOWN","at":"<ISO instant with offset>"},
                  {"state":"ABSENT"} when the user says there is none
  target_at?: soft "would like to finish by" instant
  actionable_from?: cannot/should not start before this instant
  remind_at?: when to be reminded about this task
  importance?: LOW|NORMAL|HIGH|CRITICAL ("важно" = HIGH, "очень срочно" = CRITICAL)
  category?: HOMEWORK|EXAM|LESSON|WORK|ADMIN|ERRAND|PERSONAL_APPOINTMENT|MEETING|GENERAL
  splittable?: true when the work can be done in several sittings; then
  min_chunk_minutes?/max_chunk_minutes?: sitting length bounds
CREATE_EVENT payload: something that happens at a fixed time with a start and an end
  (class, lecture, lesson, meeting, call, training, appointment, "с 21 до 22 провести
  занятие", "в 18:00 созвон на час"): {title, starts_at, ends_at, duration_minutes,
  description?, category?, importance?, remind_before_minutes? (0-1440)}. TITLE is semantic output,
  not input text with regex fragments removed. The duration must equal ends_at − starts_at (default 60
  minutes when only a start is given); a fixed-time event has no deadline and no effort estimate.
CREATE_REMINDER payload: just a moment to get attention, nothing to plan ("напомни купить хлеб
  завтра в 18", "разбуди меня в 7", "напомни и поставь будильник"): {title, remind_at,
  delivery? PUSH|ALARM|PUSH_AND_ALARM ("будильник"/"разбуди" = ALARM, "напомни … и поставь
  будильник" = PUSH_AND_ALARM), wake_check? (true for waking up), raise_volume? (true only
  for an alarm the user asked for), note?, obligation_id? (the task/event it is about)}.
Commands on existing items take obligation_id (tasks/events, from context.obligations) or
reminder_id (from context.reminders) and expected_version (that item's "version"):
  UPDATE_TASK {obligation_id, any CREATE_TASK field to change}
  UPDATE_EVENT {obligation_id, title?, starts_at?, ends_at?, remind_before_minutes?}
  UPDATE_REMINDER {reminder_id, title?, remind_at?, delivery?}
  RESCHEDULE {obligation_id|reminder_id, when: ISO instant, keep_time?: true when only a day
    was said, or temporal_transform} — "перенеси X на завтра": a task's deadline moves
    (or it is put off), an event's start moves (length kept), a reminder's moment moves.
    For a relative request with a guard and fallback, do not calculate the final timestamp.
    Use temporal_transform: {kind:"SHIFT_WITH_GUARD_AND_FALLBACK",delta_minutes,
    guard:{not_after_local_time:"HH:MM"},fallback:{relative_day:"NEXT_MORNING",
    preferred_local_time:"HH:MM",precision:"EXACT"|"APPROXIMATE"}}. For an unguarded
    shift use {kind:"RELATIVE_SHIFT",delta_minutes}. The server resolves it against the
    current entity, timezone and duration.
  SNOOZE {obligation_id|reminder_id, until} — "напомни про X через час"
  LOG_PROGRESS {obligation_id, minutes? | count?} — "поработал над X 30 минут", "сделал 3 задачи"
  COMPLETE_OBLIGATION {obligation_id|reminder_id}; CANCEL_OBLIGATION {obligation_id|reminder_id}
    ("не буду делать", "отмени"); ARCHIVE_OBLIGATION {obligation_id} ("в архив")
  REFINE_TASK {obligation_id, estimated_total_effort_minutes}
  When you cannot tell which item is meant, set payload.target_text to the words the user
  used and list "target" in unresolved_fields; the user will pick it.
COMPLETE_OBLIGATION, CANCEL_OBLIGATION and ARCHIVE_OBLIGATION always set requires_confirmation=true.
Resolve relative dates and times ("в пятницу к шести", "завтра вечером") against
context.now in context.timezone and output instants with that zone's offset. "к"/"до"/
"by"/"due"/"сдать" describe a deadline; a bare time describes when to do it
(actionable_from/target_at). If effort or deadline of a new task is not stated, leave it
out and list "estimated_total_effort_minutes" / "actual_cutoff" in unresolved_fields.
Do not invent identifiers, versions, dates, or locations."""

_TOP_LEVEL_SCHEMA = {
    "name": "botay_assistant_proposal",
    "strict": False,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "actions"],
        "properties": {
            "message": {"type": "string"},
            "read_query": {"type": ["object", "null"], "additionalProperties": True},
            "actions": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command", "payload", "confidence", "unresolved_fields", "expected_version", "requires_confirmation"],
                    "properties": {
                        "command": {"type": "string", "enum": [command for command in (
                            "CREATE_TASK", "CREATE_EVENT", "CREATE_REMINDER", "CREATE_NOTE", "UPDATE_TASK",
                            "UPDATE_EVENT", "UPDATE_REMINDER", "RESCHEDULE", "SNOOZE", "LOG_PROGRESS",
                            "COMPLETE_OBLIGATION", "CANCEL_OBLIGATION", "ARCHIVE_OBLIGATION", "REFINE_TASK",
                            "CREATE_TIME_CONSTRAINT", "UNDO_LAST",
                        )]},
                        "payload": {"type": "object", "additionalProperties": True},
                        "client_ref": {"type": "string", "maxLength": 64},
                        "depends_on": {"type": "array", "items": {"type": "string", "maxLength": 64}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "unresolved_fields": {"type": "array", "items": {"type": "string"}},
                        "expected_version": {"type": ["integer", "null"]},
                        "requires_confirmation": {"type": "boolean"},
                        "field_provenance": {
                            "type": "object",
                            "additionalProperties": {"type": "string", "enum": ["MODEL_EXPLICIT", "MODEL_INFERRED"]},
                        },
                    },
                },
            },
        },
    },
}


class ProviderUnavailable(ValidationError):
    """The provider could not be used for ``interpret()``.

    Distinct from a malformed *proposal*: every failure here degrades capture to the
    local parser, and a malformed answer can never reach the preview. ``reason`` is a
    stable code; the message never contains the credential or the provider's body.

    ========== =============================================================
    AUTH       the key was refused (401, or 403 that blames the key)
    SERVER_BLOCKED  403 that does not blame the key: the provider refuses this
               server (e.g. its region: Groq answers any key from an
               unsupported country with a bare "Forbidden")
    NOT_FOUND  the model does not exist or this key has no access to it
    ENDPOINT   the API address does not serve this provider's API
    RATE_LIMITED  too many requests right now (429)
    QUOTA      the account behind the key has no credit/quota left
    FORMAT     the provider refused the request shape interpret() needs
               (JSON output mode, parameters) or the model did not answer
               with the typed-action JSON
    MALFORMED  the HTTP answer is not this provider's API response shape
    REJECTED   any other refusal of the request (4xx)
    UPSTREAM   the provider failed (5xx, overloaded)
    NETWORK    no connection, DNS, TLS or timeout
    REQUEST    the request could not even be built (e.g. an illegal header), or
               the operator's LLM egress proxy/relay is misconfigured or refused it
    BLOCKED_URL  a user-supplied address points into a private network
    ========== =============================================================

    ``retry_after`` is the provider's Retry-After in whole seconds (429/503), if any.
    ``route`` is the egress path taken (DIRECT, DIRECT_PINNED, PROXY, RELAY) or None
    when the request never left the server.
    """

    def __init__(self, message: str, reason: str = "NETWORK", http_status: int | None = None,
                 *, retry_after: int | None = None, route: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status
        self.retry_after = retry_after
        self.route = route


def _error_fields(response: httpx.Response) -> tuple[bool, str]:
    """(is a JSON provider error, lower-cased type/code/param/message for matching).

    The text is only matched against keywords here; it is never logged or returned.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - not JSON (an HTML error page, a proxy)
        return False, ""
    if not isinstance(body, dict):
        return False, ""
    error = body.get("error")
    parts: list[str] = []
    if isinstance(error, dict):
        parts = [str(error.get(key) or "") for key in ("type", "code", "param", "status", "message")]
    elif isinstance(error, str):
        parts = [error, str(body.get("message") or "")]
    elif body.get("type") == "error" or "message" in body:
        parts = [str(body.get("type") or ""), str(body.get("message") or "")]
    else:
        return False, ""
    return True, " ".join(parts).lower()


# What a 403 says when it is about the credential rather than about who is asking.
_KEY_WORDS = ("key", "auth", "token", "credential", "permission", "unauthorized")


def _llm_egress_proxy(url: str) -> str | None:
    """Return an operator-controlled proxy for this exact provider host.

    Some providers accept a user's key from their laptop but reject the VPS egress
    network or country. BYOK keys must still stay server-side, so the supported fix
    is a narrowly-scoped outbound proxy rather than handing the saved key back to a
    browser or mobile client.

    The proxy is opt-in twice: a proxy URL must be configured and the request host
    must be listed in SEOS_LLM_EGRESS_PROXY_HOSTS. This prevents an arbitrary
    user-supplied OpenAI-compatible URL from gaining access to an operator proxy.
    """
    inline = os.environ.get("SEOS_LLM_EGRESS_PROXY", "").strip()
    file_name = os.environ.get("SEOS_LLM_EGRESS_PROXY_FILE", "").strip()
    if inline and file_name:
        raise ProviderUnavailable("configure only one LLM egress proxy source", "REQUEST")
    proxy = inline
    if file_name:
        try:
            proxy = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError:
            raise ProviderUnavailable("the LLM egress proxy configuration is unreadable", "REQUEST") from None
    if not proxy:
        return None

    allowed = {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("SEOS_LLM_EGRESS_PROXY_HOSTS", "").split(",")
        if item.strip()
    }
    if not allowed:
        raise ProviderUnavailable("LLM egress proxy hosts are not configured", "REQUEST")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host not in allowed:
        return None

    parts = urlsplit(proxy)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderUnavailable("the LLM egress proxy must be an http(s) URL", "REQUEST")
    return proxy


@dataclass(frozen=True)
class _Relay:
    url: str  # the relay endpoint this request is sent to
    # repr=False: the operator secret never appears in a traceback or log line.
    token: str = field(repr=False)


# X-SEOS-Relay-Error codes the relay itself produces. Anything else it reports is an
# operator configuration problem (REQUEST), never the user's key.
_RELAY_ERRORS = {"provider_unreachable": "NETWORK", "upstream_redirect": "UPSTREAM"}


def _llm_egress_relay(url: str) -> _Relay | None:
    """Return the operator's application relay for this exact provider host.

    An alternative to ``_llm_egress_proxy`` for providers that reject the VPS network:
    the request is sent to ``SEOS_LLM_EGRESS_RELAY_URL`` + the provider path, with the
    relay secret from ``SEOS_LLM_EGRESS_RELAY_TOKEN_FILE``, and the relay forwards it to
    its own hard-coded provider endpoint. Unlike an HTTP CONNECT proxy the relay
    terminates TLS, so it can see the user's key; it is therefore used only for hosts
    listed exactly in ``SEOS_LLM_EGRESS_RELAY_HOSTS``.
    """
    base = os.environ.get("SEOS_LLM_EGRESS_RELAY_URL", "").strip()
    if not base:
        return None
    allowed = {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("SEOS_LLM_EGRESS_RELAY_HOSTS", "").split(",")
        if item.strip()
    }
    if not allowed:
        raise ProviderUnavailable("LLM egress relay hosts are not configured", "REQUEST")
    target = urlsplit(url)
    host = (target.hostname or "").lower().rstrip(".")
    if host not in allowed or target.scheme != "https" or target.port not in (None, 443):
        return None
    if target.query or target.fragment:
        raise ProviderUnavailable("the provider address cannot be relayed", "REQUEST")

    relay = urlsplit(base)
    if relay.query or relay.fragment or relay.path not in ("", "/"):
        raise ProviderUnavailable("the LLM egress relay must be a bare https:// origin", "REQUEST")
    endpoint = f"{base.rstrip('/')}{target.path}"
    try:
        assert_public_base_url(endpoint)
    except ProviderUnavailable as exc:
        if exc.reason != "BLOCKED_URL":
            raise
        raise ProviderUnavailable("the LLM egress relay must be a public https:// URL", "REQUEST") from None

    file_name = os.environ.get("SEOS_LLM_EGRESS_RELAY_TOKEN_FILE", "").strip()
    if not file_name:
        raise ProviderUnavailable("the LLM egress relay token file is not configured", "REQUEST")
    try:
        token = Path(file_name).read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderUnavailable("the LLM egress relay token is unreadable", "REQUEST") from None
    if not token:
        raise ProviderUnavailable("the LLM egress relay token is empty", "REQUEST")
    return _Relay(endpoint, token)


def _reason(status: int, response: httpx.Response | None = None, *, custom_address: bool = False) -> str:
    provider_error, text = _error_fields(response) if response is not None else (False, "")
    mentions_model = "model" in text
    quota = any(word in text for word in ("insufficient_quota", "quota", "billing", "credit balance", "credits"))
    if status == 429 and "rate_limit" in text and "insufficient_quota" not in text:
        # Groq's per-minute/per-day limits say "rate_limit_exceeded" and append an
        # upsell link to .../settings/billing: that is a transient limit, not an
        # exhausted account, and must neither stick on a key nor trigger key failover.
        return "RATE_LIMITED"
    if status in (401, 403):
        # Providers such as Groq use 403 for organization/project model
        # permissions. That says the credential was understood but this model is
        # unavailable to it; do not mislabel it as a bad key.
        if mentions_model and any(word in text for word in (
            "not_found", "access", "permission", "blocked", "not allowed",
        )):
            return "NOT_FOUND"
        if status == 403 and not any(word in text for word in _KEY_WORDS):
            return "SERVER_BLOCKED"
        return "AUTH"
    if status == 402 or (status in (400, 429) and quota):
        return "QUOTA"
    if status == 429:
        return "RATE_LIMITED"
    if status == 404:
        # A 404 from a user-supplied address that is not even a JSON API error means
        # the address is wrong; otherwise the endpoint is right and the model is not.
        if custom_address and not provider_error:
            return "ENDPOINT"
        return "NOT_FOUND"
    if status in (400, 422):
        if any(word in text for word in ("response_format", "json_object", "json mode", "json_mode", "json_validate", "temperature",
                                          "unsupported parameter", "unsupported_parameter", "not supported")):
            return "FORMAT"
        if mentions_model and any(word in text for word in ("not found", "not_found", "does not exist", "invalid model",
                                                            "unknown model", "model_not_found", "decommissioned")):
            return "NOT_FOUND"
        return "REJECTED"
    if 300 <= status < 400:
        return "ENDPOINT"  # redirects are not followed; the address is not the API itself
    return "UPSTREAM" if status >= 500 else "REJECTED"


def assert_public_base_url(url: str) -> tuple[IpAddress, ...] | None:
    """Refuse a user-supplied API address that points into the server's own network.

    A per-user base URL makes the server issue requests on the user's behalf, so it
    must not reach loopback, private, link-local (cloud metadata) or other
    non-global addresses. Checked when saved and again before every request.

    Returns the validated address set; a direct request pins its connection to it
    (see ``netguard``), so DNS cannot change between this check and the connect.
    ``None`` only in the local-development override.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ProviderUnavailable("the API address must be an https:// URL without credentials", "BLOCKED_URL")
    try:
        port = parts.port or 443
    except ValueError:
        raise ProviderUnavailable("the API address must be an https:// URL without credentials", "BLOCKED_URL") from None
    if os.environ.get("SEOS_LLM_ALLOW_PRIVATE_BASE_URL") == "1":  # local development only
        return None
    try:
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ProviderUnavailable("the API address could not be resolved", "NETWORK") from exc
    addresses: set[IpAddress] = set()
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not is_public_address(address):
            raise ProviderUnavailable("the API address must be a public internet host", "BLOCKED_URL")
        addresses.add(address)
    if not addresses:
        raise ProviderUnavailable("the API address could not be resolved", "NETWORK")
    return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


def _retry_after(response: httpx.Response) -> int | None:
    """Retry-After (delta-seconds or HTTP-date) as bounded whole seconds, else None."""
    raw = response.headers.get("retry-after")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):  # "nan", "inf", "1e309": not a usable delay
        return None
    return max(0, min(3600, math.ceil(seconds)))


def _content_json(text: str) -> dict[str, Any]:
    """The model's text as the typed-action JSON interpret() needs, or FORMAT."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        raise ProviderUnavailable("the model did not answer with JSON", "FORMAT") from None
    if not isinstance(value, dict) or not isinstance(value.get("actions"), list):
        raise ProviderUnavailable("the model's answer has no actions list", "FORMAT")
    return value


def _field(response: httpx.Response, path: tuple[Any, ...], name: str) -> Any:
    """A value inside the provider's JSON response, or MALFORMED when it is not there."""
    try:
        value: Any = response.json()
        for key in path:
            value = value[key]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderUnavailable(f"assistant provider {name} returned an unexpected response", "MALFORMED") from None
    return value


# What the connection test asks: the same system prompt and request shape as
# interpret(), about an input whose meaning is not in doubt.
PROBE_TEXT = "Задача: проверка связи, 15 минут"
_PROBE_COMMANDS = {"CREATE_TASK", "CREATE_EVENT", "CREATE_REMINDER"}


def check_probe_answer(value: dict[str, Any]) -> None:
    """The probe must come back as at least one well-formed typed create action."""
    actions = value.get("actions") or []
    for action in actions:
        if (isinstance(action, dict) and action.get("command") in _PROBE_COMMANDS
                and isinstance(action.get("payload"), dict)
                and isinstance(action["payload"].get("title"), str) and action["payload"]["title"].strip()):
            return
    raise ProviderUnavailable("the model did not return a typed action for a plain request", "FORMAT")


def probe_context(now: str) -> dict[str, object]:
    return {"now": now, "timezone": "UTC", "obligations": [], "locale": "ru"}


def _log_request(name: str, route: str | None, started: float, result: str, http_status: int | None) -> None:
    # Operator diagnostics only: provider id, egress route, outcome class and timing.
    # Never the URL, key, relay token, prompt or provider body.
    _log.log(logging.INFO if result == "OK" else logging.WARNING,
             "llm_request provider=%s route=%s result=%s http_status=%s latency_ms=%d",
             name, route or "NONE", result, http_status if http_status is not None else "-",
             round((time.monotonic() - started) * 1000))


def _post(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, name: str,
          public_only: bool = False, custom_address: bool = False,
          observed: dict[str, Any] | None = None) -> httpx.Response:
    started = time.monotonic()
    route: str | None = None
    try:
        response, route, relayed = _send(url, headers=headers, body=body, timeout=timeout, name=name,
                                         public_only=public_only)
        if observed is not None:
            observed["route"] = route
        relay_error = response.headers.get("X-SEOS-Relay-Error") if relayed else None
        if relay_error and response.status_code >= 300:
            # The relay refused or failed by itself; the provider never saw the request,
            # so this must not read as a verdict on the user's key.
            raise ProviderUnavailable(
                f"the LLM egress relay for assistant provider {name} answered HTTP {response.status_code}",
                _RELAY_ERRORS.get(relay_error.strip().lower(), "REQUEST"),
                response.status_code, route=route,
            )
        if response.status_code >= 300:
            # The provider's body is not echoed: some providers quote part of the key.
            raise ProviderUnavailable(
                f"assistant provider {name} answered HTTP {response.status_code}",
                _reason(response.status_code, response, custom_address=custom_address),
                response.status_code, retry_after=_retry_after(response), route=route,
            )
    except ProviderUnavailable as exc:
        if exc.route is None:
            exc.route = route
        _log_request(name, exc.route, started, exc.reason, exc.http_status)
        raise
    _log_request(name, route, started, "OK", response.status_code)
    return response


def _post_pinned(url: str, addresses: tuple[IpAddress, ...], *, headers: dict[str, str],
                 json: dict[str, Any], timeout: float, follow_redirects: bool) -> httpx.Response:
    """POST over a connection that may only reach ``addresses`` (validated just before)."""
    port = urlsplit(url).port or 443
    transport = PinnedTransport(PinnedNetworkBackend(wire_host(url), addresses, port=port))
    with httpx.Client(transport=transport, trust_env=False, timeout=timeout,
                      follow_redirects=follow_redirects) as client:
        return client.post(url, headers=headers, json=json)


def _send(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, name: str,
          public_only: bool) -> tuple[httpx.Response, str, bool]:
    """Choose exactly one egress route and send; returns (response, route, relayed).

    Routes are deterministic and never come from ambient ``HTTPS_PROXY``-style
    variables (``trust_env=False`` everywhere): an operator selects PROXY or RELAY
    per exact host; everything else is DIRECT. A user-supplied address is
    DIRECT_PINNED: connected only to the addresses validated just before.
    """
    pinned = assert_public_base_url(url) if public_only else None  # before any egress choice
    proxy = _llm_egress_proxy(url)
    relay = _llm_egress_relay(url)
    if proxy and relay:
        raise ProviderUnavailable("configure only one LLM egress mechanism", "REQUEST")
    route = "PROXY" if proxy else "RELAY" if relay else "DIRECT_PINNED" if pinned else "DIRECT"
    try:
        # Redirects are not followed: a redirect must not carry the key elsewhere.
        # A configured egress proxy or relay is used only for an explicit host
        # allowlist. It changes the network origin without moving the BYOK credential
        # to the client.
        if proxy:
            with httpx.Client(proxy=proxy, trust_env=False, timeout=timeout, follow_redirects=False) as client:
                response = client.post(url, headers=headers, json=body)
        elif relay:
            with httpx.Client(trust_env=False, timeout=timeout, follow_redirects=False) as client:
                response = client.post(relay.url, headers={**headers, "X-SEOS-Relay-Token": relay.token}, json=body)
        elif pinned:
            response = _post_pinned(url, pinned, headers=headers, json=body, timeout=timeout,
                                    follow_redirects=False)
        else:
            response = httpx.post(url, headers=headers, json=body, timeout=timeout,
                                  follow_redirects=False, trust_env=False)
    except httpx.ConnectTimeout:
        raise ProviderUnavailable(f"assistant provider {name} could not be reached in time", "NETWORK",
                                  route=route) from None
    except httpx.TimeoutException:
        raise ProviderUnavailable(f"assistant provider {name} did not answer in time", "TIMEOUT", route=route) from None
    except (httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.InvalidURL, UnicodeError):
        # Refused by httpx before anything was sent: a bug here, not the network.
        raise ProviderUnavailable(f"the request to assistant provider {name} could not be built", "REQUEST",
                                  route=route) from None
    except httpx.HTTPError:
        raise ProviderUnavailable(f"assistant provider {name} is unavailable", "NETWORK", route=route) from None
    return response, route, bool(relay)


def _user_message(text: str, context: dict[str, object]) -> str:
    return json.dumps({"text": text, "context": context}, ensure_ascii=False, default=str)


@dataclass
class OpenAICompatibleProvider:
    # repr=False: a provider object in a traceback or log line never shows the key.
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    name: str = "openai"
    timeout: float = 30.0
    public_only: bool = False  # user-supplied base URL: refuse non-public hosts
    max_output_tokens: int | None = None
    last_usage: dict[str, int] | None = field(default=None, init=False, repr=False)
    # Egress route of the last request that got an HTTP answer (diagnostics only).
    last_route: str | None = field(default=None, init=False, repr=False)

    def _chat(self, messages: list[dict[str, str]], **extra: Any) -> httpx.Response:
        body: dict[str, Any] = {"model": self.model, "messages": messages, **extra}
        observed: dict[str, Any] = {}
        try:
            return _post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                body=body, timeout=self.timeout, name=self.name, public_only=self.public_only,
                custom_address=self.public_only or self.name == "openai-compatible", observed=observed,
            )
        finally:
            self.last_route = observed.get("route")

    def _complete(self, text: str, context: dict[str, object], *, repair_feedback: str | None = None) -> dict[str, Any]:
        # No temperature: OpenAI reasoning models reject a non-default one, and Groq's
        # gpt-oss fails JSON mode at temperature 0 on some inputs every time. The output
        # is validated field by field anyway, so determinism is not relied upon there.
        self.last_usage = None
        extra: dict[str, Any] = {"response_format": (
            {"type": "json_schema", "json_schema": _TOP_LEVEL_SCHEMA}
            if self.name == "openai" else {"type": "json_object"}
        )}
        if self.max_output_tokens is not None:
            extra["max_tokens"] = self.max_output_tokens
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(text, context)},
        ]
        if repair_feedback:
            messages.append({"role": "system", "content": (
                "The previous proposal was rejected by deterministic validation with code "
                f"{repair_feedback}. Return one complete replacement proposal; never alter authority or scope."
            )})
        response = self._chat(messages, **extra)
        self.last_usage = _openai_usage(response)
        content = _field(response, ("choices", 0, "message", "content"), self.name)
        if not isinstance(content, str):
            raise ProviderUnavailable(f"assistant provider {self.name} returned no text", "MALFORMED")
        return _content_json(content)

    def check(self, now: str = "2026-01-01T09:00:00+00:00") -> None:
        """A real interpret() round trip: key, address, model and the JSON contract."""
        check_probe_answer(self._complete(PROBE_TEXT, probe_context(now)))

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        return self._complete(text, context)

    def repair(self, text: str, context: dict[str, object], feedback: str) -> dict[str, Any]:
        return self._complete(text, context, repair_feedback=feedback)


@dataclass
class AnthropicProvider:
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.anthropic.com"
    name: str = "anthropic"
    timeout: float = 30.0
    public_only: bool = False
    max_output_tokens: int = 1200
    last_usage: dict[str, int] | None = field(default=None, init=False, repr=False)
    last_route: str | None = field(default=None, init=False, repr=False)

    def _messages(self, body: dict[str, Any]) -> httpx.Response:
        observed: dict[str, Any] = {}
        try:
            return _post(
                f"{self.base_url.rstrip('/')}/v1/messages",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                body={"model": self.model, **body}, timeout=self.timeout, name=self.name,
                public_only=self.public_only, custom_address=self.public_only, observed=observed,
            )
        finally:
            self.last_route = observed.get("route")

    def _complete(self, text: str, context: dict[str, object], *, repair_feedback: str | None = None) -> dict[str, Any]:
        self.last_usage = None
        system = SYSTEM_PROMPT
        if repair_feedback:
            system += ("\nThe previous proposal was rejected by deterministic validation with code "
                       f"{repair_feedback}. Return one complete replacement proposal; never alter authority or scope.")
        response = self._messages({
            "max_tokens": self.max_output_tokens, "temperature": 0, "system": system,
            "messages": [{"role": "user", "content": _user_message(text, context)}],
        })
        self.last_usage = _anthropic_usage(response)
        blocks = _field(response, ("content",), self.name)
        texts = [block.get("text") for block in blocks if isinstance(block, dict) and block.get("type", "text") == "text"] \
            if isinstance(blocks, list) else []
        if not texts or not isinstance(texts[0], str):
            raise ProviderUnavailable("assistant provider anthropic returned no text", "MALFORMED")
        return _content_json(texts[0])

    def check(self, now: str = "2026-01-01T09:00:00+00:00") -> None:
        check_probe_answer(self._complete(PROBE_TEXT, probe_context(now)))

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        return self._complete(text, context)

    def repair(self, text: str, context: dict[str, object], feedback: str) -> dict[str, Any]:
        return self._complete(text, context, repair_feedback=feedback)


PROVIDERS = {
    # id: (label, default base URL or None when the user must supply one)
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic", "https://api.anthropic.com"),
    "openai-compatible": ("OpenAI-compatible", None),
}
_ALIASES = {"compatible": "openai-compatible", "claude": "anthropic"}


def normalize_provider(kind: str) -> str:
    value = str(kind or "").strip().lower()
    value = _ALIASES.get(value, value)
    if value not in PROVIDERS:
        raise ValidationError("provider must be openai, anthropic, or openai-compatible")
    return value


def build_provider(kind: str, *, api_key: str, model: str, base_url: str | None = None,
                   user_supplied: bool = False, max_output_tokens: int | None = None):
    """The one constructor for every credential source (a user's own key or the platform's).

    ``user_supplied`` marks an address chosen by an account holder: it must be a
    public https host, checked again before every request.
    """
    kind = normalize_provider(kind)
    base = (base_url or "").strip() or PROVIDERS[kind][1]
    if not base:
        raise ValidationError("an API address is required for an OpenAI-compatible provider")
    public_only = user_supplied and base != PROVIDERS[kind][1]
    if kind == "anthropic":
        return AnthropicProvider(api_key, model, base, public_only=public_only,
                                 max_output_tokens=max_output_tokens or 1200)
    return OpenAICompatibleProvider(api_key, model, base, name=kind, public_only=public_only,
                                    max_output_tokens=max_output_tokens)


def _usage_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _openai_usage(response: httpx.Response) -> dict[str, int] | None:
    try:
        raw = response.json().get("usage")
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    prompt = _usage_int(raw.get("prompt_tokens"))
    completion = _usage_int(raw.get("completion_tokens"))
    total = _usage_int(raw.get("total_tokens"))
    if prompt is None or completion is None:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": max(total, prompt + completion) if total is not None else prompt + completion}


def _anthropic_usage(response: httpx.Response) -> dict[str, int] | None:
    try:
        raw = response.json().get("usage")
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    prompt = _usage_int(raw.get("input_tokens"))
    completion = _usage_int(raw.get("output_tokens"))
    if prompt is None or completion is None:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


class PlatformProviderPool:
    """Primary platform credential plus at most one useful standby attempt."""

    _ALTERNATE_REASONS = {"AUTH", "QUOTA"}

    def __init__(self, providers: list[Any]) -> None:
        if not providers:
            raise ValueError("at least one platform provider is required")
        self._providers = providers[:2]
        self.name = providers[0].name
        self.model = providers[0].model
        self.last_usage: dict[str, int] | None = None
        self.last_route: str | None = None
        self.last_credential: str | None = None  # "primary" or "standby" (diagnostics)
        self._last_index = 0

    def _attempt(self, index: int, text: str, context: dict[str, object]):
        provider = self._providers[index]
        self._last_index = index
        self.last_credential = "primary" if index == 0 else "standby"
        try:
            return provider.interpret(text, context)
        finally:
            self.last_usage = provider.last_usage
            self.last_route = getattr(provider, "last_route", None)

    def interpret(self, text: str, context: dict[str, object]):
        self.last_usage = None
        try:
            return self._attempt(0, text, context)
        except ProviderUnavailable as primary:
            if len(self._providers) == 1 or primary.reason not in self._ALTERNATE_REASONS:
                raise
        return self._attempt(1, text, context)

    def repair(self, text: str, context: dict[str, object], feedback: str):
        provider = self._providers[self._last_index]
        repair = getattr(provider, "repair", None)
        if not callable(repair):
            raise ProviderUnavailable("assistant provider cannot repair structured output", "FORMAT")
        try:
            return repair(text, context, feedback)
        finally:
            self.last_usage = provider.last_usage
            self.last_route = getattr(provider, "last_route", None)


def _platform_keys_from_environment() -> list[str]:
    files = os.environ.get("SEOS_PLATFORM_LLM_API_KEY_FILES", "").strip()
    if files:
        keys: list[str] = []
        for raw_path in files.split(",")[:2]:
            try:
                value = Path(raw_path.strip()).read_text(encoding="utf-8").strip()
                if value and "\n" not in value and "\r" not in value:
                    keys.append(value)
            except OSError:
                logging.getLogger("student_execution_os.providers").error(
                    "a platform LLM credential file is unreadable")
        return keys
    key = os.environ.get("SEOS_PLATFORM_LLM_API_KEY", "").strip()
    return [key] if key else []


def platform_provider_from_environment(*, max_output_tokens: int | None = None):
    """Platform-managed credentials, primary first and optional standby, or ``None``.

    These are the operator's own credentials. They are used only for accounts with a
    PLATFORM_MANAGED entitlement (see ``agent/credentials.py``), never as a default
    for everybody.
    """
    kind = os.environ.get("SEOS_PLATFORM_LLM_PROVIDER", "").strip()
    keys = _platform_keys_from_environment()
    model = os.environ.get("SEOS_PLATFORM_LLM_MODEL", "").strip()
    base = os.environ.get("SEOS_PLATFORM_LLM_BASE_URL", "").strip()
    if not kind or not keys or not model:
        return None
    providers = [build_provider(kind, api_key=key, model=model, base_url=base or None,
                                max_output_tokens=max_output_tokens) for key in keys]
    return providers[0] if len(providers) == 1 else PlatformProviderPool(providers)
