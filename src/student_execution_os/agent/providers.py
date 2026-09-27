"""Server-side LLM providers for the controlled Assistant preview boundary.

Providers may only return the small typed proposal schema consumed by
``SQLiteAssistantService``.  They never receive a repository, bearer token, or a
mutation tool, and therefore cannot apply their own output.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from student_execution_os.domain.errors import ValidationError


SYSTEM_PROMPT = """You interpret what a student wants to do for Student Execution OS. Return JSON only:
{"message":"short helpful response","actions":[{"command":"CREATE_TASK|CREATE_EVENT|CREATE_REMINDER|UPDATE_TASK|UPDATE_EVENT|UPDATE_REMINDER|RESCHEDULE|SNOOZE|LOG_PROGRESS|COMPLETE_OBLIGATION|CANCEL_OBLIGATION|ARCHIVE_OBLIGATION|REFINE_TASK","payload":{},"confidence":0.0,"unresolved_fields":[],"expected_version":null,"requires_confirmation":false}]}
Never claim an action was executed; every action is only a proposal the user reviews.
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
  занятие", "в 18:00 созвон на час"): {title, starts_at, ends_at, description?, category?,
  importance?, remind_before_minutes? (0-1440)}. The duration is ends_at − starts_at (default 60
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
    was said} — "перенеси X на завтра": a task's deadline moves (or it is put off), an
    event's start moves (length kept), a reminder's moment moves
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
    """

    def __init__(self, message: str, reason: str = "NETWORK", http_status: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status


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
                                                            "unknown model", "model_not_found")):
            return "NOT_FOUND"
        return "REJECTED"
    if 300 <= status < 400:
        return "ENDPOINT"  # redirects are not followed; the address is not the API itself
    return "UPSTREAM" if status >= 500 else "REJECTED"


def assert_public_base_url(url: str) -> None:
    """Refuse a user-supplied API address that points into the server's own network.

    A per-user base URL makes the server issue requests on the user's behalf, so it
    must not reach loopback, private, link-local (cloud metadata) or other
    non-global addresses. Checked when saved and again before every request.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ProviderUnavailable("the API address must be an https:// URL without credentials", "BLOCKED_URL")
    if os.environ.get("SEOS_LLM_ALLOW_PRIVATE_BASE_URL") == "1":  # local development only
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ProviderUnavailable("the API address could not be resolved", "NETWORK") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global:
            raise ProviderUnavailable("the API address must be a public internet host", "BLOCKED_URL")


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


def _post(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, name: str,
          public_only: bool = False, custom_address: bool = False) -> httpx.Response:
    if public_only:
        assert_public_base_url(url)  # the user's own address, before any egress choice
    proxy = _llm_egress_proxy(url)
    relay = _llm_egress_relay(url)
    if proxy and relay:
        raise ProviderUnavailable("configure only one LLM egress mechanism", "REQUEST")
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
        else:
            response = httpx.post(url, headers=headers, json=body, timeout=timeout, follow_redirects=False)
    except httpx.TimeoutException:
        raise ProviderUnavailable(f"assistant provider {name} did not answer in time", "NETWORK") from None
    except (httpx.LocalProtocolError, httpx.UnsupportedProtocol):
        # Refused by httpx before anything was sent: a bug here, not the network.
        raise ProviderUnavailable(f"the request to assistant provider {name} could not be built", "REQUEST") from None
    except httpx.HTTPError:
        raise ProviderUnavailable(f"assistant provider {name} is unavailable", "NETWORK") from None
    relay_error = response.headers.get("X-SEOS-Relay-Error") if relay else None
    if relay_error and response.status_code >= 300:
        # The relay refused or failed by itself; the provider never saw the request,
        # so this must not read as a verdict on the user's key.
        raise ProviderUnavailable(
            f"the LLM egress relay for assistant provider {name} answered HTTP {response.status_code}",
            _RELAY_ERRORS.get(relay_error.strip().lower(), "REQUEST"),
            response.status_code,
        )
    if response.status_code >= 300:
        # The provider's body is not echoed: some providers quote part of the key.
        raise ProviderUnavailable(
            f"assistant provider {name} answered HTTP {response.status_code}",
            _reason(response.status_code, response, custom_address=custom_address),
            response.status_code,
        )
    return response


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

    def _chat(self, messages: list[dict[str, str]], **extra: Any) -> httpx.Response:
        body: dict[str, Any] = {"model": self.model, "messages": messages, **extra}
        return _post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            body=body, timeout=self.timeout, name=self.name, public_only=self.public_only,
            custom_address=self.public_only or self.name == "openai-compatible",
        )

    def _complete(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        # No temperature: OpenAI reasoning models reject a non-default one, and Groq's
        # gpt-oss fails JSON mode at temperature 0 on some inputs every time. The output
        # is validated field by field anyway, so determinism is not relied upon there.
        self.last_usage = None
        extra: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if self.max_output_tokens is not None:
            extra["max_tokens"] = self.max_output_tokens
        response = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(text, context)},
        ], **extra)
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

    def _messages(self, body: dict[str, Any]) -> httpx.Response:
        return _post(
            f"{self.base_url.rstrip('/')}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            body={"model": self.model, **body}, timeout=self.timeout, name=self.name, public_only=self.public_only,
            custom_address=self.public_only,
        )

    def _complete(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        self.last_usage = None
        response = self._messages({
            "max_tokens": self.max_output_tokens, "temperature": 0, "system": SYSTEM_PROMPT,
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

    def interpret(self, text: str, context: dict[str, object]):
        self.last_usage = None
        try:
            result = self._providers[0].interpret(text, context)
            self.last_usage = self._providers[0].last_usage
            return result
        except ProviderUnavailable as primary:
            self.last_usage = self._providers[0].last_usage
            if len(self._providers) == 1 or primary.reason not in self._ALTERNATE_REASONS:
                raise
        try:
            result = self._providers[1].interpret(text, context)
            self.last_usage = self._providers[1].last_usage
            return result
        except ProviderUnavailable:
            self.last_usage = self._providers[1].last_usage
            raise


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
