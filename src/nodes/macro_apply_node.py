"""AgentCore Platform v1.0 — MacroApplyNode (CMN-C1-613).

The ``main`` slot — the core capability. Applies the resolved macro to the ticket via
the Zendesk API, but ONLY when the macro resolved unambiguously and there is no upstream
error. A high-impact (public-reply) macro requires an explicit confirmation in the
instruction; otherwise the write is withheld (``needs_confirmation``) and previewed.
``dry_run`` returns the projected apply without writing. A gate refusal is a valid
business outcome (status SUCCESS), not an execution error — post_process renders it.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.zendesk_client import ZendeskClient, ZendeskClientError


class MacroApplyNode(FunctionNode):
    """Guarded macro-apply write — only after a single macro resolved."""

    # INTERNAL, and it stays INTERNAL. This node applies a Zendesk macro to the customer's ticket.
    # `run_agent_marketplace()` stamps every caller VERIFIED_EXTERNAL with no surface to
    # raise it, so declaring that level here makes the change reachable by every
    # Marketplace user. CoE ruled write modes out of scope for this entry point and named
    # the lowering as the prohibited workaround (an internal ruling / the framework contract; (internal reference removed)
    # lists it as option one and rejects it).
    #
    # The S-1 gate runs in BaseNode.__call__() BEFORE execute(), so this node cannot make
    # the check itself: it would never run, and the whole invocation would end at status
    # error, which the runner raises on. Graph.add_edges() routes around it instead.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, client: ZendeskClient | None = None) -> None:
        super().__init__()
        self._client = client or ZendeskClient()

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        blocked = self._blocked_reason(state)
        if blocked:
            emit_trace_event("macro_apply_skipped", {"reason": blocked}, state)
            return {
                "apply_result": json.dumps(
                    {"applied": False, "dry_run": False, "needs_confirmation": False, "reason": blocked},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        target = json.loads(state.get("target_context") or "{}")
        macro = json.loads(state.get("selected_macro") or "{}")
        ticket_id = str(target.get("ticket_id", ""))
        macro_id = int(macro.get("id", 0))
        macro_title = macro.get("title", "")
        dry_run = bool(target.get("dry_run"))
        high_impact = bool(macro.get("has_public_comment"))
        confirmed = bool(target.get("confirm"))

        # High-impact (public-reply) macro requires explicit confirmation.
        if high_impact and not confirmed and not dry_run:
            emit_trace_event("macro_apply_needs_confirmation", {"macro_id": macro_id}, state)
            return {
                "apply_result": json.dumps(
                    {
                        "applied": False,
                        "dry_run": False,
                        "needs_confirmation": True,
                        "ticket_id": ticket_id,
                        "macro_id": macro_id,
                        "macro_title": macro_title,
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        if dry_run:
            emit_trace_event("macro_apply_dry_run", {"ticket_id": ticket_id, "macro_id": macro_id}, state)
            return {
                "apply_result": json.dumps(
                    {
                        "applied": False,
                        "dry_run": True,
                        "needs_confirmation": False,
                        "ticket_id": ticket_id,
                        "macro_id": macro_id,
                        "macro_title": macro_title,
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        # FAIL-CLOSED: a real apply is a live Zendesk write. When no ZENDESK_TOKEN is
        # provisioned (e.g. the STG smoke), never attempt it — return a safe plan-only
        # "cannot execute (no credential)" outcome at SUCCESS. (A dry_run above is a
        # pure projection and does not reach here.) This mirrors the read-side guard in
        # MacroListNode so the write path is fail-closed even if a candidate list was
        # already resolved.
        if not self._client.credential_available():
            emit_trace_event("macro_apply_no_credential", {"ticket_id": ticket_id, "macro_id": macro_id}, state)
            return {
                "validation_error": "Cannot execute: no Zendesk credential is configured.",
                "apply_result": json.dumps(
                    {
                        "applied": False,
                        "dry_run": False,
                        "needs_confirmation": False,
                        "no_credential": True,
                        "ticket_id": ticket_id,
                        "macro_id": macro_id,
                        "macro_title": macro_title,
                        "reason": "no Zendesk credential",
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            self._client.apply_macro(ticket_id, macro_id)
        except ZendeskClientError as exc:
            emit_trace_event("macro_apply_error", {"reason": str(exc)}, state)
            return {
                "validation_error": f"Macro apply failed: {exc}",
                "apply_result": json.dumps(
                    {"applied": False, "dry_run": False, "needs_confirmation": False, "reason": str(exc)},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event("macro_applied", {"ticket_id": ticket_id, "macro_id": macro_id}, state)
        return {
            "apply_result": json.dumps(
                {
                    "applied": True,
                    "dry_run": False,
                    "needs_confirmation": False,
                    "ticket_id": ticket_id,
                    "macro_id": macro_id,
                    "macro_title": macro_title,
                },
                ensure_ascii=False,
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _blocked_reason(state: dict[str, Any]) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "input rejected by security gate"
        if state.get("validation_error"):
            # state is dict[str, Any]; bind to the declared return type instead of
            # widening the signature. Every writer of validation_error stores a str.
            reason: str = state["validation_error"]
            return reason
        if not state.get("selected_macro"):
            return "no macro resolved"
        return ""
