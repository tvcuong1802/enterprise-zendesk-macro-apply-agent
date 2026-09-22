# CMN-C1-613 — Unit tests: services (parser + Zendesk client mappers/egress guard).

import pytest

from src.services.intent_parser_service import InputParseError, IntentParserService
from src.services.zendesk_client import ZendeskClient, ZendeskClientError


class TestIntentParserService:
    def setup_method(self):
        self.p = IntentParserService()

    def test_parse_the_x_macro(self):
        out = self.p.parse("Apply the First Response SLA Warning macro to ticket #12345")
        assert out["ticket_id"] == "12345"
        assert out["macro_name"] == "First Response SLA Warning"
        assert out["confirm"] is False and out["dry_run"] is False

    def test_parse_quoted_name(self):
        out = self.p.parse("apply 'SLA Warning' to ticket 999")
        assert out["macro_name"] == "SLA Warning" and out["ticket_id"] == "999"

    def test_explicit_dry_run_only(self):
        assert self.p.parse("dry run: apply the Foo Bar macro to ticket 1")["dry_run"] is True
        # A bare "preview" must NOT trigger dry_run (would silently skip a real apply).
        assert self.p.parse("preview it then apply the Foo Bar macro to ticket 1")["dry_run"] is False

    def test_confirm_detected(self):
        assert self.p.parse("apply the Foo Bar macro to ticket 1, I confirm")["confirm"] is True

    def test_hash_in_macro_name_does_not_hijack_ticket(self):
        # HIGH regression: a `#N` inside the quoted macro name must not become the
        # ticket id (would apply to the wrong ticket).
        out = self.p.parse('apply the "Priority #1 Response" macro to ticket #500')
        assert out["ticket_id"] == "500"
        assert out["macro_name"] == "Priority #1 Response"

    def test_confirm_in_macro_name_does_not_flip_confirm(self):
        # HIGH regression: a macro titled with "Confirm" must not satisfy the
        # high-impact confirmation guard without an explicit user confirmation.
        out = self.p.parse('apply the "Confirm Receipt Public Reply" macro to ticket #500')
        assert out["confirm"] is False

    def test_preview_in_macro_name_does_not_flip_dry_run(self):
        # MED regression: a macro titled "Preview Only ..." must not turn a real
        # apply into a silent dry-run.
        out = self.p.parse('apply the "Preview Only Notice" macro to ticket #5')
        assert out["dry_run"] is False

    def test_japanese_macro_name_with_particle_not_truncated(self):
        # MED regression: a JP macro name containing へ/に/を must survive intact
        # (was truncated by an unconditional split on the last particle).
        out = self.p.parse("チケット #123 に 顧客への返信 マクロ を適用")
        assert out["ticket_id"] == "123"
        assert out["macro_name"] == "顧客への返信"

    def test_japanese_instruction(self):
        out = self.p.parse("チケット #12345 に First Response SLA Warning マクロを適用してください")
        assert out["ticket_id"] == "12345"
        assert out["macro_name"] == "First Response SLA Warning"

    def test_nfkc_fullwidth_ticket(self):
        # Full-width digits normalize to ASCII via NFKC.
        out = self.p.parse("apply the Foo Bar macro to ticket ＃１２３")
        assert out["ticket_id"] == "123"

    def test_missing_ticket_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("apply the Foo Bar macro")

    def test_missing_macro_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("do something to ticket #5")

    def test_empty_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("   ")


class TestZendeskClient:
    def test_egress_guard_rejects_non_zendesk(self):
        with pytest.raises(ZendeskClientError):
            ZendeskClient(base_url="https://evil.example.com")

    def test_egress_guard_allows_zendesk_subdomain(self):
        c = ZendeskClient(subdomain="acme")
        assert c._base_url == "https://acme.zendesk.com"

    def test_url_building_guards_host(self):
        c = ZendeskClient(subdomain="acme")
        assert c._url("/api/v2/macros").startswith("https://acme.zendesk.com/")

    def test_no_account_configured_raises_on_use(self):
        c = ZendeskClient()  # no subdomain
        with pytest.raises(ZendeskClientError):
            c._url("/api/v2/macros")

    def test_normalize_macro_public_comment(self):
        raw = {"id": 7, "title": "Escalate", "active": True, "actions": [{"field": "comment_value", "value": "hi"}]}
        out = ZendeskClient.normalize_macro(raw)
        assert out == {"id": 7, "title": "Escalate", "active": True, "has_public_comment": True}

    def test_normalize_macro_no_public_comment(self):
        raw = {"id": 8, "title": "Tag only", "active": True, "actions": [{"field": "current_tags", "value": "urgent"}]}
        out = ZendeskClient.normalize_macro(raw)
        assert out["has_public_comment"] is False

    def test_normalize_macro_internal_note_is_not_public(self):
        # A comment macro explicitly set to a private/internal note must NOT be flagged
        # high-impact (it posts no customer-visible reply), so it skips the confirmation
        # gate. Guards against treating every comment_value action as public.
        raw = {
            "id": 9,
            "title": "Internal note",
            "active": True,
            "actions": [
                {"field": "comment_value", "value": "for the team"},
                {"field": "comment_mode_is_public", "value": "false"},
            ],
        }
        out = ZendeskClient.normalize_macro(raw)
        assert out["has_public_comment"] is False

    def test_normalize_macro_public_comment_default(self):
        # A comment action with no explicit mode defaults to public (fail-safe → confirm).
        raw = {"id": 10, "title": "Reply", "active": True, "actions": [{"field": "comment_value", "value": "thanks!"}]}
        assert ZendeskClient.normalize_macro(raw)["has_public_comment"] is True

    def test_apply_macro_previews_then_puts_ticket(self, monkeypatch):
        # Zendesk macro apply is a two-call operation: GET the apply/preview endpoint
        # (which does NOT persist), then PUT the returned ticket changes to persist.
        # This asserts the exact verb+path sequence so a regression back to a single
        # (non-persisting) POST cannot slip through.
        import src.services.zendesk_client as zc

        calls = []

        class _Resp:
            ok = True
            status_code = 200

            def __init__(self, payload=None):
                self._payload = payload or {}

            def json(self):
                return self._payload

        def fake_get(url, headers=None, timeout=None):
            calls.append(("GET", url))
            return _Resp({"result": {"ticket": {"status": "solved"}, "comment": {"body": "hi"}}})

        def fake_put(url, json=None, headers=None, timeout=None):
            calls.append(("PUT", url, json))
            return _Resp({})

        c = ZendeskClient(subdomain="acme")
        monkeypatch.setattr(c, "_headers", lambda: {})
        monkeypatch.setattr(zc.requests, "get", fake_get)
        monkeypatch.setattr(zc.requests, "put", fake_put)

        out = c.apply_macro("123", 7)
        assert out == {"ticket_id": "123", "macro_id": 7}
        assert calls[0][0] == "GET" and calls[0][1].endswith("/api/v2/tickets/123/macros/7/apply.json")
        assert calls[1][0] == "PUT" and calls[1][1].endswith("/api/v2/tickets/123.json")
        assert calls[1][2] == {"ticket": {"status": "solved"}}  # returned changes persisted

    def test_apply_macro_raises_when_no_changes(self, monkeypatch):
        import src.services.zendesk_client as zc

        class _Resp:
            ok = True
            status_code = 200

            def json(self):
                return {"result": {}}  # no ticket changes

        c = ZendeskClient(subdomain="acme")
        monkeypatch.setattr(c, "_headers", lambda: {})
        monkeypatch.setattr(zc.requests, "get", lambda *a, **k: _Resp())
        with pytest.raises(ZendeskClientError):
            c.apply_macro("123", 7)
