"""AgentCore Platform v1.0 — MacroListNode (CMN-C1-613).

List/search the account's macros so the next node can resolve the requested name to a
macro ID. Read-only; short-circuits when an upstream error / validation_error is set.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.zendesk_client import ZendeskClient, ZendeskClientError


class MacroListNode(FunctionNode):
    """Fetch account macros → macro_candidates (JSON list)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        client: ZendeskClient | None = None,
        sample_macros: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._client = client or ZendeskClient()
        # Stand-in catalogue for a deployment with no Zendesk token. Injected rather
        # than imported so a test still controls what the node sees.
        self._sample_macros = sample_macros or []

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value or state.get("validation_error"):
            emit_trace_event("macro_list_skipped", {"reason": "upstream_error"}, state)
            return {"macro_candidates": "[]", "status": AgentStatus.SUCCESS.value}

        # FAIL-CLOSED: listing macros needs the Zendesk token. When none is provisioned
        # (e.g. the STG smoke), never attempt a live Zendesk call — return an empty
        # candidate set + a graceful validation_error at SUCCESS so the pipeline
        # completes and post_process renders a "cannot execute (no credential)" notice,
        # instead of MissingSecret propagating out of require() as status=error.
        if not self._client.credential_available():
            emit_trace_event("macro_list_no_credential", {}, state)
            if self._sample_macros:
                # Demonstrate the whole flow on the bundled catalogue instead of stopping
                # here. An empty candidate set left every downstream node with nothing to
                # do, so the reply said only that a credential was missing -- correct, and
                # useless to anyone trying the template.
                return {
                    "macro_candidates": json.dumps(self._sample_macros, ensure_ascii=False),
                    "using_sample_data": "true",
                    "status": AgentStatus.SUCCESS.value,
                }
            return {
                "macro_candidates": "[]",
                "validation_error": "Cannot execute: no Zendesk credential is configured.",
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            macros = self._client.list_macros(active_only=True)
        except ZendeskClientError as exc:
            emit_trace_event("macro_list_error", {"reason": str(exc)}, state)
            return {
                "macro_candidates": "[]",
                "validation_error": f"Could not list macros: {exc}",
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event("macros_listed", {"count": len(macros)}, state)
        return {
            "macro_candidates": json.dumps(macros, ensure_ascii=False),
            "status": AgentStatus.SUCCESS.value,
        }
