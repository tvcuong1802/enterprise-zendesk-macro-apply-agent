"""AgentCore Platform v1.0 — MacroSelectNode (CMN-C1-613).

Resolve the requested macro NAME to a macro ID over the account's macro list:
exact (case-insensitive) match first, then a substring contains-match. Zero matches
is a graceful ``validation_error`` ("macro not found"); more than one match returns a
``disambiguation`` list and NEVER guesses. A single match is stored as selected_macro,
carrying the high-impact (public-reply) flag so the write node can require confirmation.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class MacroSelectNode(FunctionNode):
    """Resolve macro name → macro_id; disambiguate; flag high-impact macros."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value or state.get("validation_error"):
            emit_trace_event("macro_select_skipped", {"reason": "upstream_error"}, state)
            return {"status": AgentStatus.SUCCESS.value}

        intent = json.loads(state.get("parsed_intent") or "{}")
        name = (intent.get("macro_name") or "").strip()
        candidates = json.loads(state.get("macro_candidates") or "[]")

        matches = self._match(name, candidates)

        if not matches:
            emit_trace_event("macro_not_found", {"name_len": len(name)}, state)
            return {
                "validation_error": f"Macro not found: no macro matches '{name}'.",
                "status": AgentStatus.SUCCESS.value,
            }

        if len(matches) > 1:
            titles = [m["title"] for m in matches]
            emit_trace_event("macro_disambiguation", {"match_count": len(matches)}, state)
            return {
                "disambiguation": json.dumps(titles, ensure_ascii=False),
                "validation_error": f"Multiple macros match '{name}'; please choose one.",
                "status": AgentStatus.SUCCESS.value,
            }

        macro = matches[0]
        emit_trace_event(
            "macro_resolved",
            {"macro_id": macro["id"], "high_impact": macro["has_public_comment"]},
            state,
        )
        return {
            "selected_macro": json.dumps(
                {"id": macro["id"], "title": macro["title"], "has_public_comment": macro["has_public_comment"]},
                ensure_ascii=False,
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _match(name: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not name:
            return []
        q = name.casefold()
        norm = [(m, str(m.get("title", "")).casefold()) for m in candidates]
        exact = [m for m, t in norm if t == q]
        if exact:
            return exact
        return [m for m, t in norm if q in t]
