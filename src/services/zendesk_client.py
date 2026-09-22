"""AgentCore Platform v1.0"""

# ZendeskClient — CMN-C1-613
# Read macros and apply a macro to a ticket over the Zendesk REST API.
# - Credentials come from the bound secret provider ONLY
#   (current_secrets().require("ZENDESK_TOKEN")) — never from state or os.environ.
# - S-3 egress guard: only a *.zendesk.com host is permitted, so a misconfiguration
#   cannot exfiltrate the token off-platform.
# Constructor-injected into MacroListNode, MacroSelectNode and MacroApplyNode.
# MUST NOT be instantiated inside execute().

from __future__ import annotations

from typing import Any, Callable

from urllib.parse import urlsplit

import requests

from framework.secrets.context import current_secrets

_ALLOWED_HOST_SUFFIX = ".zendesk.com"
_TIMEOUT = 15  # seconds


class ZendeskClientError(RuntimeError):
    """Raised on a non-recoverable Zendesk API error."""


class ZendeskClient:
    """Thin Zendesk REST client for macro listing + application.

    ``subdomain`` selects the account host ``https://{subdomain}.zendesk.com``; the
    S-3 egress guard rejects any host that is not a ``*.zendesk.com`` host so the
    token cannot leak off-platform.
    """

    def __init__(self, subdomain: str = "", base_url: str = "", timeout: int = _TIMEOUT) -> None:
        if base_url:
            self._base_url = base_url.rstrip("/")
        elif subdomain:
            self._base_url = f"https://{subdomain}.zendesk.com"
        else:
            # No account configured yet — deferred; guarded on first use.
            self._base_url = ""
        self._timeout = timeout
        if self._base_url:
            self._guard_egress(self._base_url)

    @staticmethod
    def _guard_egress(url: str) -> None:
        host = urlsplit(url).hostname or ""
        if not (host == "zendesk.com" or host.endswith(_ALLOWED_HOST_SUFFIX)):
            raise ZendeskClientError(
                f"S-3 egress guard: host '{host}' is not permitted (only *{_ALLOWED_HOST_SUFFIX})."
            )

    @staticmethod
    def credential_available() -> bool:
        """True when the Zendesk API token is provisioned in the bound secret provider.

        Non-raising probe (``get`` returns ``None`` on a miss) used by the caller to
        FAIL-CLOSED before any live HTTP call when no credential is present — e.g. the
        STG smoke, which has no ``ZENDESK_TOKEN`` and must not attempt a real Zendesk
        call. The token is never cached on the instance; this only checks presence.
        """
        return bool(current_secrets().get("ZENDESK_TOKEN"))

    def _require_base(self: Any) -> str:
        if not self._base_url:
            raise ZendeskClientError("No Zendesk account configured (set config.zendesk_subdomain).")
        pinned: str = self._base_url
        return pinned

    def _headers(self: Any) -> dict[str, Any]:
        # Token fetched per call from the bound provider; never cached on self.
        token = current_secrets().require("ZENDESK_TOKEN")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        url = f"{self._require_base()}{path}"
        self._guard_egress(url)
        return url

    # ── Read ────────────────────────────────────────────────────────────────
    @staticmethod
    def _wrap_transport(fn: Callable[[], requests.Response], what: str) -> requests.Response:
        """Run a `requests.*` call, mapping requests.RequestException (Timeout,
        ConnectionError — the common Zendesk failures) to ZendeskClientError so
        it surfaces through the nodes' `except ZendeskClientError` graceful path
        instead of escaping as an uncaught error."""
        try:
            return fn()
        except requests.RequestException as exc:
            raise ZendeskClientError(f"Zendesk API transport error on {what}: {type(exc).__name__}") from exc

    def list_macros(self, active_only: bool = True) -> list[dict[str, Any]]:
        """Return the account's macros normalized to our dimensions."""
        path = "/api/v2/macros" + ("?active=true" if active_only else "")
        url = self._url(path)
        resp = self._wrap_transport(
            lambda: requests.get(url, headers=self._headers(), timeout=self._timeout), "GET macros"
        )
        if not resp.ok:
            raise ZendeskClientError(f"GET macros failed ({resp.status_code}).")
        return [self.normalize_macro(m) for m in (resp.json().get("macros") or [])]

    # ── Write ────────────────────────────────────────────────────────────────
    def apply_macro(self, ticket_id: str, macro_id: int) -> dict[str, Any]:
        """Apply ``macro_id`` to ``ticket_id``.

        Zendesk's macro "apply" endpoint is a PREVIEW (GET): it returns the ticket as
        it *would* look after the macro, without persisting anything. To actually apply
        the macro we then PUT the returned ticket changes back to the ticket. This is
        the documented two-call apply ("Show Changes to Ticket" → update the ticket) —
        a POST to the apply path does not persist the change.
        """
        preview_url = self._url(f"/api/v2/tickets/{ticket_id}/macros/{macro_id}/apply.json")
        preview = self._wrap_transport(
            lambda: requests.get(preview_url, headers=self._headers(), timeout=self._timeout),
            f"preview macro {macro_id}",
        )
        if not preview.ok:
            raise ZendeskClientError(f"preview macro {macro_id} on ticket {ticket_id} failed ({preview.status_code}).")
        ticket_changes = ((preview.json() or {}).get("result") or {}).get("ticket")
        if not ticket_changes:
            raise ZendeskClientError(f"macro {macro_id} produced no ticket changes for ticket {ticket_id}.")
        update_url = self._url(f"/api/v2/tickets/{ticket_id}.json")
        resp = self._wrap_transport(
            lambda: requests.put(
                update_url, json={"ticket": ticket_changes}, headers=self._headers(), timeout=self._timeout
            ),
            f"apply macro {macro_id}",
        )
        if not resp.ok:
            raise ZendeskClientError(f"apply macro {macro_id} to ticket {ticket_id} failed ({resp.status_code}).")
        return {"ticket_id": str(ticket_id), "macro_id": int(macro_id)}

    # ── Mapping helper (pure — unit-testable without HTTP) ───────────────────
    @staticmethod
    def normalize_macro(raw: dict[str, Any]) -> dict[str, Any]:
        """Map a Zendesk macro object to our canonical dimensions.

        ``has_public_comment`` is true when any action sets a public comment — such a
        macro posts a customer-visible reply and is treated as high-impact.
        """
        actions = raw.get("actions") or []
        has_comment = False
        explicitly_private = False
        for a in actions:
            field = a.get("field")
            value = str(a.get("value", "")).lower()
            if field == "comment_mode_is_public":
                # An explicit private/internal-note flag suppresses the public-reply impact.
                if value in ("false", "is_private", "0", "no"):
                    explicitly_private = True
                elif value in ("true", "is_public", "1", "yes"):
                    explicitly_private = False
            if field in ("comment_value", "comment_value_html"):
                has_comment = True
        # A comment action posts a customer-visible reply (high-impact) unless the macro
        # explicitly sets the comment to a private/internal note.
        has_public = has_comment and not explicitly_private
        return {
            "id": int(raw.get("id", 0)),
            "title": str(raw.get("title", "")),
            "active": bool(raw.get("active", True)),
            "has_public_comment": bool(has_public),
        }
