"""AgentCore Platform v1.0 — PostProcessNode (CMN-C1-613).

Render an EN / JA / bilingual confirmation, disambiguation prompt, needs-confirmation
preview, dry-run preview, or refusal report from the gate verdicts and apply result,
and enforce the S-3 output boundary (no Zendesk token value may cross into the report).
Japanese output uses controlled support terminology (用語統制) and keigo, with a
machine-translation disclaimer (認証された翻訳ではありません). Macro names and ticket
subjects are preserved verbatim.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from src.services.write_scope import caller_may_write
from shared.utils.audit_logger import emit_trace_event

# S-3: Zendesk token shapes must never appear in the rendered report.
_TOKEN_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+/token:[A-Za-z0-9]{20,}"),  # email/token:APITOKEN
    re.compile(r"\b[A-Za-z0-9]{40,}\b"),  # long opaque API tokens
]
_JA_DISCLAIMER = "※ 本要約は機械生成であり、認証された翻訳ではありません。"


class PostProcessNode(FunctionNode):
    """Build the EN/JA confirmation/refusal report; S-3 output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        language = state.get("output_language", "en") or "en"

        if not state.get("apply_result") and not caller_may_write(state):
            # BOTH halves are required. "no result" alone is not "the gate refused" --
            # a disambiguation, a dry run, or a blocked precondition also leave the key
            # empty, and those must still render their own report. Only a caller who
            # cannot write on this channel gets the refusal notice. Name the action that did not happen rather than reporting a generic
            # failure -- a reader told "failed" cannot tell a refusal from a bug, and will
            # retry against a change they believe was never made.
            from src.services.write_scope import write_unavailable_notice  # noqa: PLC0415

            notice = write_unavailable_notice(
                language,
                action_en="The macro application",
                action_ja="マクロの適用",
                channel_en="an authorised company support system",
                channel_ja="社内の権限あるサポート システム",
            )
            emit_trace_event("macro_report_refused", {"language": language}, state)
            # The same keys the normal path returns -- a branch that answers with a
            # DIFFERENT shape breaks every consumer that indexes into the result, and the
            # break only shows on the refusal path.
            return {
                "confirmation_report": notice,
                "formatted_output": notice,
                "status": AgentStatus.SUCCESS.value,
            }
        report_en = self._render_en(state)
        report_ja = self._render_ja(state)

        if language == "ja":
            report = report_ja
        elif language == "bilingual":
            report = f"{report_en}\n\n---\n\n{report_ja}"
        else:
            report = report_en

        emit_trace_event("report_compiled", {"language": language}, state)
        # get_output() surfaces `formatted_output` (or `result`) — write that so
        # invoke() returns the report; the outer graph does NOT override get_output.
        return {
            "confirmation_report": report,
            "formatted_output": report,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        # S-3: redact any residual Zendesk token value from all string outputs.
        for key in ("confirmation_report", "formatted_output"):
            val = result.get(key)
            if isinstance(val, str):
                for pat in _TOKEN_PATTERNS:
                    val = pat.sub("[REDACTED-CREDENTIAL]", val)
                result[key] = val
        return result

    # ── Outcome classification ────────────────────────────────────────────────
    @staticmethod
    def _outcome(state: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        target = json.loads(state.get("target_context") or "{}")
        apply = json.loads(state.get("apply_result") or "{}")
        ticket = str(target.get("ticket_id", "?"))
        disambig = json.loads(state.get("disambiguation") or "[]")
        if disambig:
            kind = "disambiguation"
        elif apply.get("needs_confirmation"):
            kind = "needs_confirmation"
        elif apply.get("no_credential"):
            # No Zendesk credential is not the same as a bad request. The node already
            # resolved which macro would be applied to which ticket; showing that plan is
            # an answer, and "Cannot execute: no Zendesk credential is configured" alone
            # is not. Ordered ABOVE the validation_error branch, which would otherwise
            # swallow it -- the node sets both.
            kind = "no_credential"
        elif state.get("validation_error"):
            kind = "error"
        elif apply.get("dry_run"):
            kind = "dry_run"
        elif apply.get("applied"):
            kind = "applied"
        else:
            kind = "error"
        return kind, ticket, apply

    def _render_en(self, state: dict[str, Any]) -> str:
        kind, ticket, apply = self._outcome(state)
        macro = apply.get("macro_title", "?")
        if kind == "disambiguation":
            titles = json.loads(state.get("disambiguation") or "[]")
            opts = "\n".join(f"- {t}" for t in titles)
            return (
                f"# Macro Apply — Disambiguation Needed\n\n**Ticket:** #{ticket}\n\n"
                f"Several macros match that name. Please specify which one:\n{opts}"
            )
        if kind == "needs_confirmation":
            return (
                f"# Macro Apply — Confirmation Required\n\n**Ticket:** #{ticket}\n\n"
                f"The macro **{macro}** posts a public (customer-visible) reply. "
                f"Re-issue the instruction with an explicit confirmation to apply it."
            )
        if kind == "no_credential":
            return (
                f"# Macro Apply — Plan Only (no changes written)\n\n**Ticket:** #{ticket}\n\n"
                f"_No Zendesk credential is available to this deployment, so this is the "
                f"plan rather than a result. Nothing was applied._\n\n"
                f"Would apply macro **{macro}** (id {apply.get('macro_id', '?')}) to ticket "
                f"#{ticket}."
            )
        if kind == "error":
            return (
                f"# Macro Apply — Not Applied\n\n**Ticket:** #{ticket}\n\n"
                f"{state.get('validation_error', 'The request could not be processed.')}"
            )
        if kind == "dry_run":
            return (
                f"# Macro Apply — Dry Run (no changes written)\n\n**Ticket:** #{ticket}\n\n"
                f"Would apply macro **{macro}** (id {apply.get('macro_id', '?')})."
            )
        return (
            f"# Macro Apply — Applied\n\n**Ticket:** #{ticket}\n\n"
            f"Applied macro **{macro}** (id {apply.get('macro_id', '?')})."
        )

    def _render_ja(self, state: dict[str, Any]) -> str:
        kind, ticket, apply = self._outcome(state)
        macro = apply.get("macro_title", "?")
        if kind == "disambiguation":
            titles = json.loads(state.get("disambiguation") or "[]")
            opts = "\n".join(f"- {t}" for t in titles)
            return (
                f"# マクロ適用 — 候補の確認\n\n**チケット:** #{ticket}\n\n"
                f"該当するマクロが複数見つかりました。いずれかをご指定ください：\n{opts}\n\n{_JA_DISCLAIMER}"
            )
        if kind == "needs_confirmation":
            return (
                f"# マクロ適用 — 確認が必要です\n\n**チケット:** #{ticket}\n\n"
                f"マクロ「{macro}」は顧客に公開される返信を投稿します。適用するには、"
                f"明示的な確認を付けて指示を再送してください。\n\n{_JA_DISCLAIMER}"
            )
        if kind == "error":
            return (
                f"# マクロ適用 — 未適用\n\n**チケット:** #{ticket}\n\n"
                f"リクエストを処理できませんでした。\n\n{_JA_DISCLAIMER}"
            )
        if kind == "dry_run":
            return (
                f"# マクロ適用 — ドライラン（変更は書き込まれていません）\n\n**チケット:** #{ticket}\n\n"
                f"マクロ「{macro}」（id {apply.get('macro_id', '?')}）を適用予定です。\n\n{_JA_DISCLAIMER}"
            )
        return (
            f"# マクロ適用 — 適用完了\n\n**チケット:** #{ticket}\n\n"
            f"マクロ「{macro}」（id {apply.get('macro_id', '?')}）を適用いたしました。\n\n{_JA_DISCLAIMER}"
        )
