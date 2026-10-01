"""The Assistant and agent actions, and the account's model settings."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from student_execution_os.agent import (
    ActionRequest,
    AgentCommand,
    IntentStrength,
    SQLiteActionGateway,
    LlmCredentialStore,
    SQLiteAssistantService,
)

from .common import _jsonify

from .base import ApplicationService


class AssistantService(ApplicationService):
    """The Assistant and agent actions, and the account's model settings."""

    def agent_cancel_preview(self, obligation_id: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            gateway = SQLiteActionGateway(repo)
            ob = gateway.read_obligation(principal=self.principal, obligation_id=obligation_id)
            intent = gateway.mint_intent(
                principal=self.principal,
                command=AgentCommand.CANCEL_OBLIGATION,
                target_entity_id=obligation_id,
                intent_strength=IntentStrength.AMBIGUOUS,
                expected_version=expected_version,
            )
            return {
                "intent_id": intent.id,
                "command": intent.command.value,
                "target_entity_id": obligation_id,
                "target_title": ob.title,
                "expected_version": expected_version,
                "requires_confirmation": intent.requires_confirmation,
                "scope": "one obligation",
                "effect": "Lifecycle becomes CANCELLED; evidence history remains unchanged; current plan becomes stale.",
            }

    def assistant_interpret(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            credentials = LlmCredentialStore(repo)
            resolved = credentials.resolve(self.account_id)
            service = SQLiteAssistantService(repo, self.principal, provider=resolved.provider)
            result = service.interpret(str(payload.get("text", "")), payload.get("context"), degrade_invalid=True)
            if resolved.source.value == "USER_BYOK":
                credentials.record_use(self.account_id, resolved.version, service.provider_failure)
            result["credential_source"] = resolved.source.value
            return result

    def assistant_capabilities(self) -> dict[str, Any]:
        with self._repo() as repo:
            resolved = LlmCredentialStore(repo).resolve(self.account_id)
        live = resolved.live
        return {
            "live_llm_provider": live,
            "credential_source": resolved.source.value,
            "provider": resolved.provider.name if live else None,
            "model": resolved.provider.model if live else None,
            "credential_status": resolved.status,
            "structured_actions": True,
            "confirmation_required": True,
            "degraded_mode": not live,
        }

    def llm_settings(self) -> dict[str, Any]:
        with self._repo() as repo:
            return LlmCredentialStore(repo).public(self.account_id)

    def save_llm_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return LlmCredentialStore(repo).save(self.account_id, payload)

    def delete_llm_settings(self) -> dict[str, Any]:
        with self._repo() as repo:
            return LlmCredentialStore(repo).delete(self.account_id)

    def test_llm_settings(self) -> dict[str, Any]:
        with self._repo() as repo:
            return LlmCredentialStore(repo).test(self.account_id)

    def assistant_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAssistantService(repo, self.principal).apply(payload)

    def assistant_undo(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAssistantService(repo, self.principal).undo(payload)

    def agent_cancel_confirm_execute(self, intent_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._repo() as repo:
            gateway = SQLiteActionGateway(repo)
            intent = gateway.confirm_intent(principal=self.principal, intent_id=intent_id)
            result = gateway.execute_cancel(
                principal=self.principal,
                request=ActionRequest(
                    intent_id=intent.id,
                    idempotency_key=idempotency_key or f"web:{intent.id}:{uuid4()}",
                    expected_version=intent.expected_version,
                ),
            )
            return _jsonify(result)
