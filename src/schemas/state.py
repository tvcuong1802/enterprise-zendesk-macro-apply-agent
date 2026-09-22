"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only.
#
# SAFETY CONTRACT (CMN-C1-613):
# - All complex objects are stored as JSON-serialized str (never raw dict/list) so
#   the checkpoint stays msgpack-safe.
# - No Zendesk token / credential in state (checkpoint DB leakage) — the token is
#   fetched at call time via current_secrets().require("ZENDESK_TOKEN") and never
#   persisted.
# - InvocationContext is accessed via config["configurable"] / InvocationContext
#   .from_state(state) only, never stored in state.

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Zendesk macro-apply state for CMN-C1-613.

    Shared fields (user_input, validated_input, status, session_id, node_history,
    error_log, formatted_output, result, hitl_*, caller_trust_level, etc.) are
    inherited from AgentState and are NOT redeclared here.

    Field ownership:
        PreProcessNode  -> parsed_intent, target_context, validation_error, output_language
        MacroListNode   -> macro_candidates
        MacroSelectNode -> selected_macro, disambiguation, (validation_error)
        MacroApplyNode  -> apply_result
        PostProcessNode -> confirmation_report, formatted_output (inherited; surfaced by get_output)
    """

    # True when the reply was built on the BUNDLED SAMPLE macro catalogue rather
    # than a real Zendesk. DECLARED because LangGraph merges only declared fields.
    using_sample_data: NotRequired[str]  # "true" when set; this repo keeps State flat-str

    # The language the model decided this reader wants, carried to the trailer.
    # Declared because LangGraph merges only declared fields -- an undeclared key is
    # dropped between nodes and the decision would be computed and lost.
    answer_language: str

    # ── PreProcessNode outputs ─────────────────────────────────────────────
    # JSON: {"ticket_id": str, "macro_name": str, "confirm": bool, "dry_run": bool}
    parsed_intent: NotRequired[str]  # default ""
    # JSON: {"ticket_id": str, "confirm": bool, "dry_run": bool}
    target_context: NotRequired[str]  # default ""
    # Non-empty string signals a business-invalid input; downstream nodes
    # short-circuit and post_process renders a refusal/error report.
    validation_error: NotRequired[str]  # default ""
    # Output rendering language: "en" | "ja" | "bilingual" (default "en").
    output_language: NotRequired[str]  # default "en"

    # ── MacroListNode output ───────────────────────────────────────────────
    # JSON list: [{"id": int, "title": str, "active": bool, "has_public_comment": bool}]
    macro_candidates: NotRequired[str]  # default "[]"

    # ── MacroSelectNode outputs ────────────────────────────────────────────
    # JSON: {"id": int, "title": str, "has_public_comment": bool} — the resolved macro.
    selected_macro: NotRequired[str]  # default ""
    # JSON list of candidate titles when more than one macro matched (never guess).
    disambiguation: NotRequired[str]  # default "[]"

    # ── MacroApplyNode (main slot) output ──────────────────────────────────
    # JSON: {"applied": bool, "dry_run": bool, "needs_confirmation": bool,
    #        "ticket_id": str, "macro_id": int, "macro_title": str}
    apply_result: NotRequired[str]  # default ""

    # ── PostProcessNode output ─────────────────────────────────────────────
    # Final Markdown confirmation/disambiguation/refusal report. S-3 gate verifies
    # no token value crosses this boundary. `formatted_output` (inherited from
    # AgentState) carries the same report and is what get_output() surfaces.
    confirmation_report: NotRequired[str]  # default ""
