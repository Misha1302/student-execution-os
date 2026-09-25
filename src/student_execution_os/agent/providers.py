"""Server-side LLM providers for the controlled Assistant preview boundary.

Providers may only return the small typed proposal schema consumed by
``SQLiteAssistantService``.  They never receive a repository, bearer token, or a
mutation tool, and therefore cannot apply their own output.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from student_execution_os.domain.errors import ValidationError


SYSTEM_PROMPT = """You interpret what a student wants to do for Student Execution OS. Return JSON only:
{"message":"short helpful response","actions":[{"command":"CREATE_TASK|CREATE_EVENT|REFINE_TASK|LOG_PROGRESS|COMPLETE_OBLIGATION|CANCEL_OBLIGATION","payload":{},"confidence":0.0,"unresolved_fields":[],"expected_version":null,"requires_confirmation":false}]}
Never claim an action was executed; every action is only a proposal the user reviews.
CREATE_TASK payload (omit what the user did not say; no other keys are accepted):
  title: short imperative title in the user's language ("Сдать лабораторную по физике")
  description?: extra details the user gave
  estimated_total_effort_minutes?: whole minutes ("часа два" = 120, "полчаса" = 30)
  actual_cutoff?: the hard deadline: {"state":"KNOWN","at":"<ISO instant with offset>"},
                  {"state":"ABSENT"} when the user says there is none
  target_at?: soft "would like to finish by" instant
  actionable_from?: cannot/should not start before this instant
  remind_at?: when the user asked to be reminded ("напомни завтра вечером")
  importance?: LOW|NORMAL|HIGH|CRITICAL ("важно" = HIGH, "очень срочно" = CRITICAL)
  category?: HOMEWORK|EXAM|LESSON|WORK|ADMIN|ERRAND|PERSONAL_APPOINTMENT|MEETING|GENERAL
  splittable?: true when the work can be done in several sittings; then
  min_chunk_minutes?/max_chunk_minutes?: sitting length bounds
CREATE_EVENT {title, starts_at, ends_at}; REFINE_TASK {obligation_id,
estimated_total_effort_minutes}; LOG_PROGRESS {obligation_id, minutes};
COMPLETE_OBLIGATION / CANCEL_OBLIGATION {obligation_id}.
Resolve relative dates and times ("в пятницу к шести", "завтра вечером") against
context.now in context.timezone and output instants with that zone's offset. "к"/"до"/
"by"/"due"/"сдать" describe a deadline; a bare time describes when to do it
(actionable_from/target_at). If effort or deadline is not stated, leave it out and
list "estimated_total_effort_minutes" / "actual_cutoff" in unresolved_fields.
Commands on existing items must take obligation_id and expected_version (its
"version") from context.obligations; COMPLETE_OBLIGATION and CANCEL_OBLIGATION
always set requires_confirmation=true. Do not invent identifiers, versions, dates,
or locations."""


class ProviderUnavailable(ValidationError):
    """The provider could not be reached or answered with an HTTP error.

    Distinct from malformed output: an outage degrades to the local parser, while a
    malformed answer is rejected so it can never reach the preview. ``reason`` is a
    stable code (AUTH, RATE_LIMITED, NOT_FOUND, REJECTED, UPSTREAM, NETWORK,
    BLOCKED_URL); the message never contains the credential or the provider's body.
    """

    def __init__(self, message: str, reason: str = "NETWORK") -> None:
        super().__init__(message)
        self.reason = reason


def _reason(status: int) -> str:
    if status in (401, 403):
        return "AUTH"
    if status == 429:
        return "RATE_LIMITED"
    if status == 404:
        return "NOT_FOUND"
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
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise ValidationError("assistant provider returned invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("actions"), list):
        raise ValidationError("assistant provider response must contain an actions list")
    return value


def _post(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, name: str,
          public_only: bool = False) -> httpx.Response:
    if public_only:
        assert_public_base_url(url)
    try:
        # Redirects are not followed: a redirect must not carry the key elsewhere.
        response = httpx.post(url, headers=headers, json=body, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        raise ProviderUnavailable(f"assistant provider {name} is unavailable", "NETWORK") from None
    if response.status_code >= 300:
        # The provider's body is not echoed: some providers quote part of the key.
        raise ProviderUnavailable(
            f"assistant provider {name} answered HTTP {response.status_code}", _reason(response.status_code)
        )
    return response


def _user_message(text: str, context: dict[str, object]) -> str:
    return json.dumps({"text": text, "context": context}, ensure_ascii=False, default=str)


CHECK_PROMPT = "Reply with the single word OK."


@dataclass
class OpenAICompatibleProvider:
    # repr=False: a provider object in a traceback or log line never shows the key.
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    name: str = "openai"
    timeout: float = 30.0
    public_only: bool = False  # user-supplied base URL: refuse non-public hosts

    def _chat(self, messages: list[dict[str, str]], **extra: Any) -> httpx.Response:
        body: dict[str, Any] = {"model": self.model, "messages": messages, **extra}
        return _post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            body=body, timeout=self.timeout, name=self.name, public_only=self.public_only,
        )

    def check(self) -> None:
        """One minimal request that proves key, model and endpoint work together."""
        self._chat([{"role": "user", "content": CHECK_PROMPT}])

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        # Current OpenAI reasoning models reject a non-default temperature; output is
        # validated field by field anyway, so determinism is not relied upon there.
        extra: dict[str, Any] = {} if self.name == "openai" else {"temperature": 0}
        response = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(text, context)},
        ], response_format={"type": "json_object"}, **extra)
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError(f"assistant provider {self.name} returned an unexpected response") from exc
        return _content_json(str(content))


@dataclass
class AnthropicProvider:
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.anthropic.com"
    name: str = "anthropic"
    timeout: float = 30.0
    public_only: bool = False

    def _messages(self, body: dict[str, Any]) -> httpx.Response:
        return _post(
            f"{self.base_url.rstrip('/')}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            body={"model": self.model, **body}, timeout=self.timeout, name=self.name, public_only=self.public_only,
        )

    def check(self) -> None:
        self._messages({"max_tokens": 8, "messages": [{"role": "user", "content": CHECK_PROMPT}]})

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        response = self._messages({
            "max_tokens": 1200, "temperature": 0, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _user_message(text, context)}],
        })
        try:
            content = response.json()["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError("assistant provider anthropic returned an unexpected response") from exc
        return _content_json(str(content))


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
                   user_supplied: bool = False):
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
        return AnthropicProvider(api_key, model, base, public_only=public_only)
    return OpenAICompatibleProvider(api_key, model, base, name=kind, public_only=public_only)


def platform_provider_from_environment():
    """Platform-managed credentials (future paid tier), or ``None``.

    These are the operator's own credentials. They are used only for accounts with a
    PLATFORM_MANAGED entitlement (see ``agent/credentials.py``), never as a default
    for everybody.
    """
    kind = os.environ.get("SEOS_PLATFORM_LLM_PROVIDER", "").strip()
    key = os.environ.get("SEOS_PLATFORM_LLM_API_KEY", "").strip()
    model = os.environ.get("SEOS_PLATFORM_LLM_MODEL", "").strip()
    base = os.environ.get("SEOS_PLATFORM_LLM_BASE_URL", "").strip()
    if not kind or not key or not model:
        return None
    return build_provider(kind, api_key=key, model=model, base_url=base or None)
