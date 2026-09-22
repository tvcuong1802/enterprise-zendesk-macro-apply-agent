# CMN-C1-613 — Integration tests: full graph invoke with a mocked Zendesk client.

from framework.schemas.invocation_context import InvocationContext, TrustLevel
from framework.secrets.context import NullProvider, bound_secrets

from src.graph.graph import Graph

MACROS = [
    {"id": 11, "title": "First Response SLA Warning", "active": True, "has_public_comment": False},
    {"id": 22, "title": "Escalate to Tier 2", "active": True, "has_public_comment": True},
    {"id": 33, "title": "SLA Warning Level 2", "active": True, "has_public_comment": False},
]


class FakeClient:
    """No HTTP / no secrets — injected after compile()."""

    def __init__(self, macros=None, raise_on_apply=False, has_credential=True):
        self._macros = macros if macros is not None else MACROS
        self._raise_on_apply = raise_on_apply
        self._has_credential = has_credential
        self.apply_calls = []

    def credential_available(self):
        return self._has_credential

    def list_macros(self, active_only=True):
        return self._macros

    def apply_macro(self, ticket_id, macro_id):
        if self._raise_on_apply:
            from src.services.zendesk_client import ZendeskClientError

            raise ZendeskClientError("apply boom")
        self.apply_calls.append((ticket_id, macro_id))
        return {"ticket_id": ticket_id, "macro_id": macro_id}


def _build(raise_on_apply=False):
    g = Graph()
    g.compile()
    client = FakeClient(raise_on_apply=raise_on_apply)
    g._nodes["macro_list"]._client = client
    g._nodes["main"]._client = client
    return g, client


#: A caller who is actually permitted to write. The Marketplace runner grants
#: VERIFIED_EXTERNAL to every user and the write node requires INTERNAL, so a write-path
#: test run at the old default no longer exercises the write -- it exercises the refusal,
#: which has its own test at the bottom of this file.
_WRITER = TrustLevel.INTERNAL


def _run(g, text, trust=_WRITER):
    ctx = InvocationContext(session_id="it", caller_trust_level=trust, caller_id="it")
    with bound_secrets(NullProvider()):
        return g.invoke(text, ctx=ctx, input_context={"instruction": text})


def test_valid_apply():
    g, client = _build()
    out = _run(g, "Apply the First Response SLA Warning macro to ticket #12345")
    assert out["status"] == "success"
    assert "Applied" in out["output"]
    assert client.apply_calls == [("12345", 11)]


def test_high_impact_requires_confirmation_no_write():
    g, client = _build()
    out = _run(g, "apply the Escalate to Tier 2 macro to ticket 77")
    assert "Confirmation Required" in out["output"]
    assert client.apply_calls == []


def test_high_impact_confirmed_writes():
    g, client = _build()
    _run(g, "apply the Escalate to Tier 2 macro to ticket 77, I confirm")
    assert client.apply_calls == [("77", 22)]


def test_dry_run_previews_without_writing():
    g, client = _build()
    out = _run(g, "dry run: apply the First Response SLA Warning macro to ticket 5")
    assert "Dry Run" in out["output"]
    assert client.apply_calls == []


def test_ambiguous_name_disambiguation_no_write():
    g, client = _build()
    out = _run(g, "apply the 'SLA Warning' macro to ticket 3")
    assert "Disambiguation" in out["output"]
    assert client.apply_calls == []


def test_macro_not_found_no_write():
    g, client = _build()
    out = _run(g, "apply the Nonexistent Foo macro to ticket 9")
    assert out["status"] == "success"
    assert "Not Applied" in out["output"]
    assert client.apply_calls == []


def test_empty_input_graceful():
    g, client = _build()
    out = _run(g, "")
    assert out["status"] == "success"  # graceful, not error
    assert "Not Applied" in out["output"]
    assert client.apply_calls == []


def test_japanese_output_has_disclaimer():
    g, _ = _build()
    out = _run(g, "チケット #12345 に First Response SLA Warning マクロを適用してください")
    assert "認証された翻訳ではありません" in out["output"]


def test_under_trust_caller_refused_no_write():
    # S-1: an ANONYMOUS caller cannot mutate a ticket → ERROR state, not a raise.
    g, client = _build()
    out = _run(g, "apply the First Response SLA Warning macro to ticket #1", trust=TrustLevel.ANONYMOUS)
    assert str(out["status"]).lower().endswith("error")
    assert client.apply_calls == []


def test_apply_error_surfaced_not_crash():
    g, client = _build(raise_on_apply=True)
    out = _run(g, "apply the First Response SLA Warning macro to ticket #12345")
    # Write failure is surfaced as a not-applied report, not an unhandled crash.
    assert out["status"] == "success"
    assert "Not Applied" in out["output"]


def test_a_marketplace_caller_cannot_apply_a_macro():
    """The ruling, end to end.

    VERIFIED_EXTERNAL is exactly what `run_agent_marketplace()` stamps on every caller. If
    this run performed the change, any Marketplace user could make it. SUCCESS, not error:
    the runner raises on anything else and the caller would be shown "agent failed" for a
    request that was merely not permitted.
    """
    g, client = _build()
    out = _run(g, "apply macro First Response SLA Warning to ticket 3", trust=TrustLevel.VERIFIED_EXTERNAL)

    assert out["status"] == "success"
    assert not out.get("apply_result"), "the write happened"
    report = str(out.get("output") or out.get("formatted_output") or "")
    assert "was not performed" in report or "実行していません" in report, report[:300]
    for leak in ("TrustLevel", "VERIFIED_EXTERNAL", "INTERNAL", "token"):
        assert leak not in report, f"the refusal names {leak}"

