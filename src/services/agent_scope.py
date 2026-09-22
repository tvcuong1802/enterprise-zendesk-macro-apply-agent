"""What THIS agent's output must not be used for, and how it reads a request.

Kept in its own module so the wording lives beside the agent it describes, while the
MECHANISM stays byte-identical fleet-wide in ``disclaimer.py`` and ``input_intake.py``.
A generic "AI-generated draft" is true of every template here and tells a reader nothing
they can act on; this names the decisions the output must not stand in for.
"""

from __future__ import annotations

SCOPE_EN = "Reference only: a record of a Zendesk macro application \u2014 which macro was resolved, and what the instruction asked for. It is not a support decision, not an approval to reply publicly, and not a substitute for reading the ticket. Check the ticket before relying on it."

SCOPE_JA = (
    " "
    "参考情報です。Zendesk マクロの適用記録であり、どのマクロが特定されたかと指示内容を示すものです。サポート上の判断でも、公開返信の承認でも、チケット確認に代わるものでもありません。ご利用前にチケットをご確認ください。"
)


# Names that stay in Latin script inside a Japanese answer -- they are names, not
# English prose, and a language check that counts them gets this agent wrong.
LANGUAGE_POLICY: dict[str, object] = {
    "identifiers": ("Zendesk",),
}


# What the intake call reads out of a free-form request. No `fields` are declared:
# this agent's own extractors already recover what it needs, and a second extractor
# would be a second source of truth. What the call adds is the language of the answer
# -- a request typed in romanised Japanese is entirely Latin, and reading the
# characters gets that reader wrong.
INTAKE_POLICY: dict[str, object] = {
    "languages": ("en", "ja"),
    "default_language": "en",
    "fields": {},
    "capabilities": (
        "Apply a named Zendesk macro to a ticket from a plain-language instruction",
        "Say which macro the name resolved to, and list the candidates when several match",
        "Flag a macro that posts a public reply as high-impact",
    ),
    "examples": (
        {
            "message": "Apply the 'Refund approved' macro to ticket 4821.",
            "expect": {"language": "en", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "チケット 4821 に「返金承認」マクロを適用してください。",
            "expect": {"language": "ja", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "Decide whether this customer deserves a refund.",
            "expect": {"language": "en", "fields": {}, "fits": "no", "suggestion": 1},
        },
    ),
}

# Re-exported so every call site reads `from src.services.agent_scope import
# resolve_answer_language` -- the wording above is per agent, the mechanism is not, and it
# lives in language_decision.py where one patch fixes every repo.
from src.services.language_decision import (  # noqa: E402
    language_instruction,
    resolve_answer_language,
)

__all__ = [
    "INTAKE_POLICY",
    "LANGUAGE_POLICY",
    "SCOPE_EN",
    "SCOPE_JA",
    "language_instruction",
    "resolve_answer_language",
]
