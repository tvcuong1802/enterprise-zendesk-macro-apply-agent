# CMN-C1-613 — Framework compliance tests (TC-01..08).

import inspect

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext, TrustLevel
from framework.secrets.context import NullProvider, bound_secrets

from src.graph.graph import Graph
from src.nodes.macro_apply_node import MacroApplyNode
from src.nodes.pre_process_node import PreProcessNode
from framework.schemas.agent_state import AgentState

from src.schemas.state import State

ERROR = AgentStatus.ERROR.value


# ── TC-01: State is a flat TypedDict — primitives / JSON-string only ───────────
def test_tc01_state_flat_no_prohibited_types():
    import typing

    # Domain fields are declared NotRequired[str] (org-wide TypedDict totality rule);
    # resolve PEP 563 string annotations, then unwrap NotRequired/Required wrappers.
    hints = typing.get_type_hints(State, include_extras=True)
    # only the agent-declared fields (not inherited AgentState fields)
    own = set(State.__annotations__) - set(AgentState.__annotations__)
    assert own, "State declares no agent-specific fields"
    for name in own:
        typ = hints[name]
        if typing.get_origin(typ) in (typing.NotRequired, typing.Required):
            typ = typing.get_args(typ)[0]
        assert typ is str, f"{name} is not a flat str type: {typ}"


# ── TC-02: S-2 gate fires on unsafe input → ERROR (no raise) ───────────────────
def test_tc02_s2_rejects_unsafe_input():
    node = PreProcessNode()
    st = {
        "user_input": "apply the Foo macro to ticket #1 ../../etc/passwd",
        "input_context": {},
        "error_log": [],
        "status": "",
    }
    gated = node._extra_security_gate_input(st)
    assert gated["status"] == ERROR
    out = node.execute(gated)
    assert out["status"] == ERROR


# ── TC-03: no hardcoded credentials in src (defense; gate-credential-scan is CI) ─
def test_tc03_no_credential_literals_in_state():
    src = inspect.getsource(State)
    assert "ZENDESK_TOKEN" not in src or "require" not in src  # token only referenced in prose
    # State declares no credential-like field names
    for name in State.__annotations__:
        assert not any(k in name.lower() for k in ("token", "secret", "password", "api_key", "credential"))


# ── TC-04: InvocationContext is never stored in State ──────────────────────────
def test_tc04_no_invocationcontext_in_state():
    for typ in State.__annotations__.values():
        assert "InvocationContext" not in str(typ)


# ── TC-05: every node emits at least one domain S-4 event in execute() ─────────
def test_tc05_audit_event_emitted(monkeypatch):
    captured = []
    import src.nodes.macro_apply_node as m

    monkeypatch.setattr(m, "emit_trace_event", lambda e, p, s: captured.append(e))
    node = MacroApplyNode()
    node.execute({"validation_error": "blocked", "status": "", "node_history": [], "error_log": []})
    assert captured, "no domain emit_trace_event fired in execute()"
    assert not any(e in ("node_start", "node_complete", "node_error") for e in captured)


# ── TC-06 / TC-07: security gates are @final — overriding raises TypeError ─────
def test_tc06_security_gate_input_is_final():
    with pytest.raises(TypeError):

        class Bad(FunctionNode):  # noqa: B903
            def _security_gate_input(self, state):
                return state


def test_tc07_security_gate_output_is_final():
    with pytest.raises(TypeError):

        class Bad(FunctionNode):  # noqa: B903
            def _security_gate_output(self, result):
                return result


# ── TC-08: required_trust_level enforced — under-trust caller refused ──────────
def test_tc08_trust_level_enforced():
    g = Graph()
    g.compile()

    class FakeClient:
        def credential_available(self):
            return True

        def list_macros(self, active_only=True):
            return [{"id": 11, "title": "First Response SLA Warning", "active": True, "has_public_comment": False}]

        def apply_macro(self, ticket_id, macro_id):
            raise AssertionError("write must not be reached for an under-trusted caller")

    g._nodes["macro_list"]._client = FakeClient()
    g._nodes["main"]._client = FakeClient()
    ctx = InvocationContext(session_id="t", caller_trust_level=TrustLevel.ANONYMOUS, caller_id="t")
    with bound_secrets(NullProvider()):
        out = g.invoke(
            "apply the First Response SLA Warning macro to ticket #1",
            ctx=ctx,
            input_context={"instruction": "apply the First Response SLA Warning macro to ticket #1"},
        )
    assert str(out["status"]).lower().endswith("error")


def test_tc08b_read_nodes_at_verified_external_and_the_writer_at_internal():
    """The writer must NOT sit at VERIFIED_EXTERNAL; the read nodes must.

    This test used to assert VERIFIED_EXTERNAL on every node, which pinned exactly the
    state CoE prohibits: that level is what run_agent_marketplace() grants every
    Marketplace user, so declaring it on the node that applies a Zendesk macro to the customer's ticket makes the change reachable
    by all of them (an internal ruling / the framework contract; (internal reference removed) rejects the lowering).

    The declaration stays honest and Graph.add_edges() routes around the node for a caller
    who cannot reach INTERNAL -- see the comment there for why the check cannot live in
    the node itself.
    """
    for cls in (PreProcessNode, MacroApplyNode):
        expected = TrustLevel.INTERNAL if cls is MacroApplyNode else TrustLevel.VERIFIED_EXTERNAL
        assert cls.required_trust_level == expected, cls.__name__
