"""AgentCore Platform v1.0"""

# IntentParserService — CMN-C1-613
# Deterministic natural-language -> structured macro-apply intent parsing.
# Handles English and Japanese instructions (NFKC-normalized). Stateless;
# unit-testable in isolation. Constructor-injected into PreProcessNode.
# MUST NOT be instantiated inside execute().

from __future__ import annotations

from typing import Any

import re
import unicodedata


class InputParseError(ValueError):
    """Raised when the instruction cannot be resolved to a ticket + macro name."""


# Ticket reference: "#12345", "ticket 12345", "ticket #12345", JP "チケット #12345".
_TICKET_RE = re.compile(r"(?:ticket\s*#?\s*|チケット\s*#?\s*|#)(\d{1,12})\b", re.I)
_BARE_TICKET_RE = re.compile(r"\bticket\s+(?:number\s+)?(\d{1,12})\b", re.I)

# Macro name (English): a quoted string, or "the <Name> macro".
_QUOTED_MACRO_RE = re.compile(r"['\"“”「」]([^'\"“”「」]{1,120})['\"“”「」]")
_THE_X_MACRO_RE = re.compile(r"(?:apply\s+(?:the\s+)?|the\s+)(.+?)\s+macro\b", re.I)
# Macro name (Japanese): "<Name> マクロ" — the words before マクロ.
_JA_MACRO_RE = re.compile(r"([^\s、。][^、。]*?)\s*マクロ")

# Dry-run must be an explicit directive — a bare "preview" is too common a verb to
# treat as no-write intent (it would silently skip a real apply). Require an
# unambiguous phrase (T-8). JP: "ドライラン", "適用せず".
_DRY_RUN_RE = re.compile(
    r"\b(dry[- ]?run|dryrun|what[- ]?if|no[- ]?apply|do ?n'?t apply|"
    r"without applying|preview only|preview the change|simulate the change)\b"
    r"|ドライラン|適用せず|適用しない",
    re.I,
)
# Explicit confirmation of a high-impact (public-reply) macro. JP: "確認", "はい適用".
_CONFIRM_RE = re.compile(
    r"\b(confirm|confirmed|i confirm|yes,? apply|go ahead|approve the public reply)\b"
    r"|確認します|確認済み|はい、?適用|公開返信を承認",
    re.I,
)


class IntentParserService:
    """Parse an NL macro-apply instruction (EN or JA) into a structured intent.

    Returns ``{"ticket_id": str, "macro_name": str, "confirm": bool, "dry_run": bool}``.
    Raises ``InputParseError`` when a ticket reference or a macro name is missing —
    the agent never applies a macro it cannot unambiguously resolve.
    """

    def parse(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            raise InputParseError("Empty instruction.")

        # NFKC folds full-width digits/latin (common in Japanese input) to ASCII.
        text = unicodedata.normalize("NFKC", text)

        # Parse the macro name FIRST and blank its span out of the text used for
        # ticket / confirm / dry-run detection. Those detectors scan for `#N`,
        # "confirm", "preview only", etc.; a macro NAME legitimately containing
        # such tokens (e.g. "Priority #1 Response", "Confirm Receipt", "Preview
        # Only Notice") would otherwise hijack the ticket id (wrong-ticket write),
        # flip the high-impact confirmation guard (public reply with no consent),
        # or turn a real apply into a silent dry-run. Detecting on the residual
        # removes that whole class.
        macro_name, residual = self._parse_macro_name_and_residual(text)
        if not macro_name:
            raise InputParseError(
                "No macro name found — name the macro to apply, e.g. 'apply the First Response SLA Warning macro'."
            )

        ticket_id = self._parse_ticket(residual)
        if not ticket_id:
            raise InputParseError("No ticket reference found — expected e.g. 'ticket #12345'.")

        return {
            "ticket_id": ticket_id,
            "macro_name": macro_name,
            "confirm": bool(_CONFIRM_RE.search(residual)),
            "dry_run": bool(_DRY_RUN_RE.search(residual)),
        }

    @staticmethod
    def _parse_ticket(text: str) -> str:
        m = _TICKET_RE.search(text) or _BARE_TICKET_RE.search(text)
        return m.group(1) if m else ""

    @staticmethod
    def _parse_macro_name(text: str) -> str:
        name, _ = IntentParserService._parse_macro_name_and_residual(text)
        return name

    @staticmethod
    def _parse_macro_name_and_residual(text: str) -> tuple[str, str]:
        """Return (macro_name, residual) where residual is `text` with the
        macro-name STRING blanked out, so ticket/confirm/dry-run detection can
        run on the residual without a `#N`/"confirm"/"preview" that is part of
        the macro NAME hijacking those fields. Blanking the name string (not the
        whole regex match span) keeps any surrounding ticket clause intact —
        important for the JP form where the name-capture can run from the start
        of the string."""
        name = ""
        # A quoted name takes precedence so multi-word names survive intact.
        q = _QUOTED_MACRO_RE.search(text)
        if q:
            name = q.group(1).strip()
        if not name:
            m = _THE_X_MACRO_RE.search(text)
            if m:
                name = re.sub(r"^(?:please\s+|apply\s+|the\s+)+", "", m.group(1).strip(), flags=re.I).strip()
        if not name:
            # Japanese "<Name> マクロ".
            ja = _JA_MACRO_RE.search(text)
            if ja:
                cap = ja.group(1).strip()
                # Strip a LEADING ticket clause if the greedy capture ran past it
                # (e.g. "チケット #123 に 顧客への返信" → "顧客への返信"). Only remove a
                # prefix that actually carries a ticket ref — never split on the
                # last particle unconditionally, which truncated names legitimately
                # containing に/へ/を (e.g. "顧客への返信" → "の返信").
                name = re.sub(r"^.*?(?:チケット|#)\s*#?\s*\d{1,12}\s*(?:に|へ|を|の)?\s*", "", cap).strip() or cap
        if not name:
            return "", text
        residual = text.replace(name, " ", 1)
        return name, residual
