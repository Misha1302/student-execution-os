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
COMPLETE_OBLIGATION and CANCEL_OBLIGATION always require_confirmation=true and
must include payload.obligation_id and expected_version from supplied context;
otherwise list the missing field in unresolved_fields. Do not invent identifiers,
versions, dates, or locations. Use ISO-8601 instants with offsets."""


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


@dataclass
class OpenAICompatibleProvider:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    name: str = "openai"
    timeout: float = 30.0

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"text": text, "context": context}, ensure_ascii=False)},
                ],
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError(f"assistant provider {self.name} is unavailable") from exc
        return _content_json(str(content))


@dataclass
class AnthropicProvider:
    api_key: str
    model: str
    base_url: str = "https://api.anthropic.com"
    name: str = "anthropic"
    timeout: float = 30.0

    def interpret(self, text: str, context: dict[str, object]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.model, "max_tokens": 1200, "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps({"text": text, "context": context}, ensure_ascii=False)}],
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
            content = response.json()["content"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError("assistant provider anthropic is unavailable") from exc
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
    provider = provider_from_environment()
    return {
        "live_llm_provider": provider is not None,
        "provider": None if provider is None else provider.name,
        "model": None if provider is None else provider.model,
        "structured_actions": True,
        "confirmation_required": True,
        "degraded_mode": provider is None,
    }
