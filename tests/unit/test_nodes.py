# CMN-C1-613 — Unit tests: nodes (mocked Zendesk client).

import json

from framework.schemas.agent_status import AgentStatus

from src.nodes.macro_apply_node import MacroApplyNode
from src.nodes.macro_list_node import MacroListNode
from src.nodes.macro_select_node import MacroSelectNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

SUCCESS = AgentStatus.SUCCESS.value
ERROR = AgentStatus.ERROR.value

MACROS = [
    {"id": 11, "title": "First Response SLA Warning", "active": True, "has_public_comment": False},
    {"id": 22, "title": "Escalate to Tier 2", "active": True, "has_public_comment": True},
    {"id": 33, "title": "SLA Warning Level 2", "active": True, "has_public_comment": False},
]


class FakeClient:
    """Injectable stand-in for ZendeskClient — no HTTP, no secrets."""

    def __init__(self, macros=None, raise_on_list=False, raise_on_apply=False, has_credential=True):
        self._macros = macros if macros is not None else MACROS
        self._raise_on_list = raise_on_list
        self._raise_on_apply = raise_on_apply
        self._has_credential = has_credential
        self.apply_calls = []

    def credential_available(self):
        return self._has_credential

    def list_macros(self, active_only=True):
        if self._raise_on_list:
            from src.services.zendesk_client import ZendeskClientError

            raise ZendeskClientError("list boom")
        return self._macros

    def apply_macro(self, ticket_id, macro_id):
        if self._raise_on_apply:
            from src.services.zendesk_client import ZendeskClientError

            raise ZendeskClientError("apply boom")
        self.apply_calls.append((ticket_id, macro_id))
        return {"ticket_id": ticket_id, "macro_id": macro_id}


def _base_state(**kw):
    st = {"user_input": "", "input_context": {}, "node_history": [], "error_log": [], "status": ""}
    st.update(kw)
    return st


class TestPreProcessNode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_valid_parse(self):
        st = _base_state(user_input="Apply the First Response SLA Warning macro to ticket #12345")
        out = self.node.execute(st)
        assert out["validation_error"] == ""
        assert json.loads(out["parsed_intent"])["macro_name"] == "First Response SLA Warning"
        assert json.loads(out["target_context"])["ticket_id"] == "12345"
        assert out["status"] == SUCCESS

    def test_reads_from_input_context_first(self):
        st = _base_state(
            user_input="[MASKED] SLA Warning macro ticket #1",
            input_context={"instruction": "apply the First Response SLA Warning macro to ticket #1"},
        )
        out = self.node.execute(st)
        assert json.loads(out["parsed_intent"])["macro_name"] == "First Response SLA Warning"

    def test_unparseable_sets_validation_error_graceful(self):
        st = _base_state(user_input="hello there, nothing actionable")
        out = self.node.execute(st)
        assert out["validation_error"]
        assert out["status"] == SUCCESS  # graceful, not ERROR

    def test_s2_gate_rejects_control_chars(self):
        st = _base_state(user_input="apply the Foo macro to ticket #1 ../../etc")
        gated = self.node._extra_security_gate_input(st)
        assert gated["status"] == ERROR
        out = self.node.execute(gated)
        assert out["status"] == ERROR
        assert "security gate" in out["validation_error"].lower()

    def test_japanese_sets_language(self):
        st = _base_state(user_input="チケット #5 に Foo マクロを適用")
        out = self.node.execute(st)
        assert out["output_language"] == "ja"


class TestMacroListNode:
    def test_lists_candidates(self):
        node = MacroListNode(client=FakeClient())
        out = node.execute(_base_state(target_context=json.dumps({"ticket_id": "1"})))
        assert len(json.loads(out["macro_candidates"])) == 3

    def test_skip_on_validation_error(self):
        node = MacroListNode(client=FakeClient())
        out = node.execute(_base_state(validation_error="bad"))
        assert out["macro_candidates"] == "[]"

    def test_list_error_becomes_validation_error(self):
        node = MacroListNode(client=FakeClient(raise_on_list=True))
        out = node.execute(_base_state(target_context=json.dumps({"ticket_id": "1"})))
        assert out["validation_error"]

    def test_no_credential_fail_closed_no_call(self):
        # STG smoke: no ZENDESK_TOKEN -> fail-closed, empty candidates + validation_error
        # at SUCCESS, and list_macros() (the live call) is never reached.
        client = FakeClient(raise_on_list=True, has_credential=False)  # would raise if called
        out = MacroListNode(client=client).execute(_base_state(target_context=json.dumps({"ticket_id": "1"})))
        assert out["macro_candidates"] == "[]"
        assert out["validation_error"]
        assert out["status"] == AgentStatus.SUCCESS.value


class TestMacroSelectNode:
    def setup_method(self):
        self.node = MacroSelectNode()

    def _state(self, name):
        return _base_state(
            parsed_intent=json.dumps({"macro_name": name}),
            macro_candidates=json.dumps(MACROS),
        )

    def test_exact_match(self):
        out = self.node.execute(self._state("First Response SLA Warning"))
        assert json.loads(out["selected_macro"])["id"] == 11

    def test_high_impact_flag(self):
        out = self.node.execute(self._state("Escalate to Tier 2"))
        assert json.loads(out["selected_macro"])["has_public_comment"] is True

    def test_ambiguous_disambiguation(self):
        out = self.node.execute(self._state("SLA Warning"))
        assert not out.get("selected_macro")
        assert len(json.loads(out["disambiguation"])) == 2
        assert out["validation_error"]

    def test_not_found(self):
        out = self.node.execute(self._state("Nonexistent Foo"))
        assert "not found" in out["validation_error"].lower()

    def test_skip_on_upstream_error(self):
        out = self.node.execute(_base_state(validation_error="bad"))
        assert not out.get("selected_macro")


class TestMacroApplyNode:
    def _ready(self, high_impact=False, confirm=False, dry_run=False):
        macro = {"id": 11, "title": "First Response SLA Warning", "has_public_comment": high_impact}
        return _base_state(
            target_context=json.dumps({"ticket_id": "12345", "confirm": confirm, "dry_run": dry_run}),
            selected_macro=json.dumps(macro),
        )

    def test_applies_when_resolved(self):
        client = FakeClient()
        out = MacroApplyNode(client=client).execute(self._ready())
        assert json.loads(out["apply_result"])["applied"] is True
        assert client.apply_calls == [("12345", 11)]

    def test_high_impact_needs_confirmation_no_write(self):
        client = FakeClient()
        out = MacroApplyNode(client=client).execute(self._ready(high_impact=True))
        r = json.loads(out["apply_result"])
        assert r["needs_confirmation"] is True and r["applied"] is False
        assert client.apply_calls == []

    def test_high_impact_confirmed_writes(self):
        client = FakeClient()
        out = MacroApplyNode(client=client).execute(self._ready(high_impact=True, confirm=True))
        assert json.loads(out["apply_result"])["applied"] is True
        assert client.apply_calls == [("12345", 11)]

    def test_dry_run_no_write(self):
        client = FakeClient()
        out = MacroApplyNode(client=client).execute(self._ready(dry_run=True))
        r = json.loads(out["apply_result"])
        assert r["dry_run"] is True and r["applied"] is False
        assert client.apply_calls == []

    def test_blocked_by_validation_error(self):
        client = FakeClient()
        out = MacroApplyNode(client=client).execute(_base_state(validation_error="macro not found"))
        assert json.loads(out["apply_result"])["applied"] is False
        assert client.apply_calls == []

    def test_apply_error_becomes_validation_error(self):
        out = MacroApplyNode(client=FakeClient(raise_on_apply=True)).execute(self._ready())
        assert out["validation_error"]
        assert json.loads(out["apply_result"])["applied"] is False

    def test_no_credential_fail_closed_no_write(self):
        # STG smoke: no ZENDESK_TOKEN -> fail-closed at the write, no apply_macro() call.
        client = FakeClient(raise_on_apply=True, has_credential=False)  # would raise if called
        out = MacroApplyNode(client=client).execute(self._ready())
        r = json.loads(out["apply_result"])
        assert r["applied"] is False and r.get("no_credential") is True
        assert client.apply_calls == []
        assert out["status"] == AgentStatus.SUCCESS.value


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def _applied(self, language="en"):
        return _base_state(
            output_language=language,
            target_context=json.dumps({"ticket_id": "12345"}),
            apply_result=json.dumps(
                {
                    "applied": True,
                    "dry_run": False,
                    "needs_confirmation": False,
                    "ticket_id": "12345",
                    "macro_id": 11,
                    "macro_title": "First Response SLA Warning",
                }
            ),
        )

    def test_applied_report_en(self):
        out = self.node.execute(self._applied())
        assert "Applied" in out["confirmation_report"]
        assert "#12345" in out["formatted_output"]

    def test_japanese_disclaimer(self):
        out = self.node.execute(self._applied(language="ja"))
        assert "認証された翻訳ではありません" in out["formatted_output"]

    def test_disambiguation_report(self):
        # A permitted caller: this test is about the disambiguation render, not the write
        # gate. Without it the bare state reads as ANONYMOUS -- correct fail-closed
        # behaviour -- and the node answers with the write-unavailable notice instead.
        st = _base_state(
            caller_trust_level="internal",
            output_language="en",
            target_context=json.dumps({"ticket_id": "3"}),
            disambiguation=json.dumps(["First Response SLA Warning", "SLA Warning Level 2"]),
            validation_error="Multiple macros match",
        )
        out = self.node.execute(st)
        assert "Disambiguation" in out["confirmation_report"]

    def test_s3_redacts_token(self):
        leaked = {
            "confirmation_report": "leaked Bearer abcdefghijklmnopqrstuvwxyz012345",
            "formatted_output": "email/token:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        }
        cleaned = self.node._extra_security_gate_output(leaked)
        assert "[REDACTED-CREDENTIAL]" in cleaned["confirmation_report"]
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in cleaned["formatted_output"]
