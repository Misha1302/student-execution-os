"""Server-side LLM providers for the controlled Assistant preview boundary.

Providers may only return the small typed proposal schema consumed by
``SQLiteAssistantService``.  They never receive a repository, bearer token, or a
mutation tool, and therefore cannot apply their own output.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from student_execution_os.domain.errors import ValidationError


SYSTEM_PROMPT = """You interpret user text for Student Execution OS. Return JSON only:
{"message":"short helpful response","actions":[{"command":"CREATE_TASK|CREATE_EVENT|REFINE_TASK|LOG_PROGRESS|COMPLETE_OBLIGATION|CANCEL_OBLIGATION","payload":{},"confidence":0.0,"unresolved_fields":[],"expected_version":null,"requires_confirmation":false}]}
Never claim an action was executed. CREATE_TASK and CREATE_EVENT are proposals.
Payloads: CREATE_TASK {title, estimated_total_effort_minutes?, description?,
importance?}; CREATE_EVENT {title, starts_at, ends_at}; REFINE_TASK {obligation_id,
estimated_total_effort_minutes}; LOG_PROGRESS {obligation_id, minutes};
COMPLETE_OBLIGATION / CANCEL_OBLIGATION {obligation_id}.
Commands on existing items must take obligation_id and expected_version (its
"version") from context.obligations; COMPLETE_OBLIGATION and CANCEL_OBLIGATION
always set requires_confirmation=true. If something is missing, list the field in
unresolved_fields. Do not invent identifiers, versions, dates, or locations. Use
ISO-8601 instants with offsets; context.now and context.timezone give the clock."""


class ProviderUnavailable(ValidationError):
    """The provider could not be reached or answered with an HTTP error.

    Distinct from malformed output: an outage degrades to the local parser, while a
    malformed answer is rejected so it can never reach the preview.
    """


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


def _post(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, name: str) -> httpx.Response:
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(f"assistant provider {name} is unavailable") from exc
    return response


def _user_message(text: str, context: dict[str, object]) -> str:
    return json.dumps({"text": text, "context": context}, ensure_ascii=False, default=str)


@dataclass
class OpenAICompatibleProvider:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    name: str = "openai"
    timeout: float = 30.0

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        response = _post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            body={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_message(text, context)},
                ],
            },
            timeout=self.timeout, name=self.name,
        )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError(f"assistant provider {self.name} returned an unexpected response") from exc
        return _content_json(str(content))


@dataclass
class AnthropicProvider:
    api_key: str
    model: str
    base_url: str = "https://api.anthropic.com"
    name: str = "anthropic"
    timeout: float = 30.0

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        response = _post(
            f"{self.base_url.rstrip('/')}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            body={
                "model": self.model, "max_tokens": 1200, "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": _user_message(text, context)}],
            },
            timeout=self.timeout, name=self.name,
        )
        try:
            content = response.json()["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError("assistant provider anthropic returned an unexpected response") from exc
        return _content_json(str(content))


def provider_from_environment():
    """Return a configured live provider, or ``None`` for the local parser.

    One generic key name keeps provider secrets server-side and avoids accidentally
    embedding vendor-specific credentials in the web bundle.
    """
    kind = os.environ.get("SEOS_LLM_PROVIDER", "").strip().lower()
    key = os.environ.get("SEOS_LLM_API_KEY", "").strip()
    model = os.environ.get("SEOS_LLM_MODEL", "").strip()
    base = os.environ.get("SEOS_LLM_BASE_URL", "").strip()
    if not kind or not key or not model:
        return None
    if kind == "openai":
        return OpenAICompatibleProvider(key, model, base or "https://api.openai.com/v1", name="openai")
    if kind in {"openai-compatible", "compatible"}:
        if not base:
            raise ValidationError("SEOS_LLM_BASE_URL is required for an OpenAI-compatible provider")
        return OpenAICompatibleProvider(key, model, base, name="openai-compatible")
    if kind in {"anthropic", "claude"}:
        return AnthropicProvider(key, model, base or "https://api.anthropic.com")
    raise ValidationError("SEOS_LLM_PROVIDER must be openai, anthropic, or openai-compatible")


def capabilities() -> dict[str, Any]:
    error = None
    try:
        provider = provider_from_environment()
    except ValidationError as exc:  # misconfiguration must not take the API down
        provider, error = None, str(exc)
    return {
        "live_llm_provider": provider is not None,
        "provider": None if provider is None else provider.name,
        "model": None if provider is None else provider.model,
        "structured_actions": True,
        "confirmation_required": True,
        "degraded_mode": provider is None,
        "configuration_error": error,
    }
