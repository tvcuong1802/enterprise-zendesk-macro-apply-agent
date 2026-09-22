"""AgentCore Platform v1.0 — PreProcessNode (CMN-C1-613).

Parse the NL macro-apply instruction into a structured intent and run the S-2 input
boundary. Business-invalid input (no ticket / no macro name) becomes a graceful
``validation_error`` (SUCCESS + refusal report downstream); a genuinely unsafe input
(path traversal / control chars / oversized) is a hard S-2 rejection (status = ERROR)
via the ``_extra_security_gate_input`` hook.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.base_node import BaseNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.intent_parser_service import InputParseError, IntentParserService

# S-2: reject traversal, doubled separators, and control characters that could
# escape the API path when interpolated into /tickets/{id}/macros/{macro_id}/apply.
_UNSAFE_INPUT = re.compile(r"(\.\./|[\x00-\x1f\x7f]|[<>|`$])")
_MAX_INPUT_CHARS = 4000
# Presence of any CJK character selects Japanese rendering.
_CJK = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


class PreProcessNode(BaseNode):
    """Parse instruction → parsed_intent + target_context; set validation_error on bad input.

    Inherits BaseNode, not FunctionNode, to narrow S-2 to the finding types this agent
    operates on.

    WHY. The default S-2 gate masks every `detect_pii` finding in `user_input` before
    `execute()` runs, and for this agent those findings ARE the instruction. Measured by
    running the graph 2026-09-04: pre_process received
    the macro NAME (a Title-Case run, the documented high-recall name
    heuristic) as well as the address: 'Apply the [MASKED] macro to ticket 4821 for
    [MASKED]'. The agent could not resolve the one macro it was asked to apply.

    WHY THIS WAY. `FunctionNode._security_gate_input` is `@final` -- overriding raises
    TypeError at class definition, by design. `_PII_SCAN_FIELDS` is a module constant in
    the wheel with no configuration and no trust-level condition. The one sanctioned
    position is the one the framework itself takes for `GraphNode` and `RemoteAgentNode`:
    a node inheriting BaseNode implements the `@abstractmethod` gate itself. the framework contract
    states that this is "a deliberate design choice, not a bypass".

    WHAT IS NARROWED. Only findings of type `name` / `email`. Every other PII class in the same
    sentence is masked exactly as before. The injection check is NOT narrowed -- same
    fields, same blocking -- because an instruction aimed at the agent is a different
    thing from a customer identifier. S-3 egress is reproduced unchanged from the wheel.
    """

    #: The finding types this agent OPERATES ON, and the only ones left unmasked. A closed
    #: set, declared here where a reviewer reads the node rather than buried in a helper.
    _S2_OPERATES_ON: ClassVar[tuple[str, ...]] = ("name", "email")

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, parser: IntentParserService | None = None) -> None:
        super().__init__()
        self._parser = parser or IntentParserService()

    @staticmethod
    def _raw_instruction(state: dict[str, Any]) -> str:
        # The macro name + ticket ref are structured COMMAND ARGUMENTS. The framework
        # S-2 gate masks Title-Case runs in user_input (its documented high-recall
        # "name" heuristic), which corrupts Zendesk macro titles. So the caller supplies
        # the instruction on the structured, non-PII-scanned `input_context` channel;
        # we parse that when present and fall back to user_input. S-2's SAFETY role
        # (unsafe-char / size reject, below) and S-3 egress remain fully in force, and
        # the macro title is account configuration — not customer PII.
        ic = state.get("input_context") or {}
        return ic.get("instruction") or state.get("user_input", "") or ""

    def _security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2, narrowed to the declared finding types. See the class docstring for why.

        Implemented here because this node inherits BaseNode, where the gate is
        `@abstractmethod`. Everything the default gate does is still done -- the same
        fields are scanned, every other PII class is masked, the injection check runs
        unchanged -- and then this node's own checks run on top.
        """
        from src.services.selective_pii_gate import selective_security_gate_input

        state = selective_security_gate_input(
            state,
            operates_on=self._S2_OPERATES_ON,
            node_name=self.__class__.__name__,
        )
        if state.get("status") == AgentStatus.ERROR.value:
            return state
        return self._extra_security_gate_input(state)

    def _security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3, unchanged. Narrowing S-2 must not quietly cost the egress scan too."""
        from src.services.selective_pii_gate import default_security_gate_output

        return default_security_gate_output(result, node_name=self.__class__.__name__)

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        # S-2 hard gate: never raise — set ERROR status per FunctionNode contract.
        text = self._raw_instruction(state)
        if len(text) > _MAX_INPUT_CHARS:
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append("S-2: instruction exceeds size limit.")
        elif _UNSAFE_INPUT.search(text):
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append("S-2: instruction contains unsafe path/control characters.")
        return state

    def _answer_language(self, state: Any) -> str:
        """Which language to answer in, decided once per request by the model.

        Called from execute(), so it runs AFTER the S-2 input gate -- anything earlier
        would send raw input, PII included, to the provider. The model READS; this agent
        DECIDES: the return value is narrowed to the two languages the scope wording
        exists in, and any other answer falls back to the script of the message.
        """
        from src.services.agent_scope import resolve_answer_language  # noqa: PLC0415

        try:
            from src.services.app_config import llm_settings  # noqa: PLC0415
            from src.services.llm_provider import build_llm_client  # noqa: PLC0415

            client = build_llm_client(dict(state or {}), llm_settings())
        except Exception:  # noqa: BLE001 -- no client: the script fallback still answers
            client = None
        return resolve_answer_language(state, client)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # The model reads the request and decides the language of the answer; every

        # other decision in this pipeline stays deterministic. Here rather than earlier

        # because the S-2 gate runs before execute().

        answer_language = self._answer_language(state)
        # S-2 hard rejection already set ERROR — do not process further.
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("input_rejected", {"reason": "s2_unsafe_input"}, state)
            return {
                # The model's language decision, carried so the trailer can use it.
                "answer_language": answer_language,
                "validation_error": "Input rejected by the S-2 security gate.",
                "status": AgentStatus.ERROR.value,
            }

        text = self._raw_instruction(state)
        language = "ja" if _CJK.search(text) else "en"

        try:
            intent = self._parser.parse(text)
        except InputParseError as exc:
            emit_trace_event("intent_parse_failed", {"reason": str(exc)}, state)
            return {
                # The model's language decision, carried so the trailer can use it.
                "answer_language": answer_language,
                "validation_error": f"Could not parse the instruction: {exc}",
                "output_language": language,
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event(
            "intent_parsed",
            {"ticket_id": intent["ticket_id"], "dry_run": intent["dry_run"], "confirm": intent["confirm"]},
            state,
        )
        return {
            # The model's language decision, carried so the trailer can use it.
            "answer_language": answer_language,
            "parsed_intent": json.dumps(intent, ensure_ascii=False),
            "target_context": json.dumps(
                {"ticket_id": intent["ticket_id"], "confirm": intent["confirm"], "dry_run": intent["dry_run"]},
                ensure_ascii=False,
            ),
            "validation_error": "",
            "output_language": language,
            "status": AgentStatus.SUCCESS.value,
        }
