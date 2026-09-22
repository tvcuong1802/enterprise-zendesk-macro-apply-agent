# 03 · Test Specification — CMN-C1-613 Enterprise Zendesk Macro Apply Agent

## Test Strategy

All tests run on the real `agenticstar-agentcore==1.0.0` wheel (CI `run-tests` arm,
wheel-era: no `stub harness`). Three layers:

- **Unit** (`tests/unit/`) — services + nodes in isolation with an injected fake Zendesk
  client (no HTTP, no secrets) + framework-compliance TCs.
- **Integration** (`tests/integration/test_graph.py`) — the full compiled graph invoked
  end-to-end via `Graph().compile()` + `invoke(user_input, ctx, input_context)`, fake
  client injected on the `macro_list` and `main` nodes.
- **Proof-of-Boundary** (`tests/proof_of_boundary/`) — framework boundary verification.

Total: **64 tests**, all green on the wheel; `ruff check src/ tests/` clean.

## Framework-Compliance Tests (`tests/unit/test_framework_compliance.py`)

| TC-ID | Test | Expected |
|---|---|---|
| TC-01 | `test_tc01_state_flat_no_prohibited_types` | every agent-declared State field is a flat `str` (JSON-serialized compound values); no `dict`/`list`/Pydantic |
| TC-02 | `test_tc02_s2_rejects_unsafe_input` | S-2 `_extra_security_gate_input` sets status ERROR on path-traversal/control chars (no raise) |
| TC-03 | `test_tc03_no_credential_literals_in_state` | no credential-like field names; token only referenced in prose (gate-credential-scan is the CI enforcer) |
| TC-04 | `test_tc04_no_invocationcontext_in_state` | State carries no `InvocationContext` annotation |
| TC-05 | `test_tc05_audit_event_emitted` | a domain `emit_trace_event` fires inside `execute()`; never `node_start/complete/error` |
| TC-06 | `test_tc06_security_gate_input_is_final` | overriding `_security_gate_input` on a FunctionNode raises `TypeError` at class definition |
| TC-07 | `test_tc07_security_gate_output_is_final` | overriding `_security_gate_output` raises `TypeError` |
| TC-08 | `test_tc08_trust_level_enforced` + `test_tc08b_...` | an ANONYMOUS caller is refused (ERROR state, no write); nodes declare VERIFIED_EXTERNAL |

## Domain Unit Tests

- **`test_services.py` (19)** — IntentParserService: EN "the X macro", quoted names,
  explicit dry-run only (bare "preview" ≠ dry-run), confirm detection, Japanese
  instruction, NFKC full-width digits, missing-ticket / missing-macro / empty raise.
  ZendeskClient: egress guard rejects non-`*.zendesk.com`, subdomain URL building,
  no-account error, two-call `apply_macro` GET-preview→PUT-ticket verb+path sequence
  (+ no-changes raise), `normalize_macro` public / non-public / internal-note classification.
- **`test_nodes.py` (23)** — PreProcessNode (parse, input_context precedence, graceful
  unparseable, S-2 reject, JA language), MacroListNode (list, skip-on-error, error→
  validation_error), MacroSelectNode (exact match, high-impact flag, ambiguous→
  disambiguation, not-found, skip-on-error), MacroApplyNode (apply, high-impact→
  needs_confirmation, confirmed write, dry-run no-write, blocked, apply-error→
  validation_error), PostProcessNode (EN report, JA disclaimer, disambiguation, S-3 redaction).

## Integration Tests (`test_graph.py`, 10)

Valid apply · high-impact requires confirmation (no write) · confirmed write · dry-run
preview (no write) · ambiguous name → disambiguation (no write) · macro not found (no
write) · empty input graceful · Japanese output has the 認証された翻訳ではありません
disclaimer · under-trust caller refused (ERROR, no write) · apply error surfaced (no crash).

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|---|---|---|---|
| PB-2 / PB-5 | State serialization / checkpoint safety | `test_state_safety.py` | State declares no Pydantic/InvocationContext/credential fields |
| PB-4 | Import isolation | `test_import_isolation.py` | AST scan: 0 Level-0 (`agenticstar`) imports |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` | every node runs S-1 → node_start → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → node_complete |

PB-1 (BaseNode→AuditLogger), PB-3 (L1→external service via ZendeskClient), and PB-7
(HITL) are covered structurally: S-4 events are asserted in TC-05/unit tests; the
external boundary is the constructor-injected `ZendeskClient` (egress-guarded); HITL is
disabled (`hitl.enabled: false`), so PB-7 is not applicable.

## Refused input — what the sender receives (shared contract, 2026-09-15)

Measured across the fleet with a real model: a message the framework's S-2 gate declined
came back as `status: error` carrying the generic line "No answer could be produced for
this request." `normalize_terminal_output()` raises on any status but SUCCESS, so the
runner discarded the whole envelope and the sender read **"agent failed"** — with nothing
to act on, and no reason to send anything different next time.

| Situation | What is returned | Why |
|---|---|---|
| S-2 declined the MESSAGE | `status: success`, `refusal_kind: "input"`, a sentence naming what to change, plus the trailer | The sender is legitimate and holds something they can fix; they only learn that if the reply reaches them |
| The agent has its own refusal wording | That wording, not the shared sentence | "The shipment could not be classified" says which step stopped; the generic line does not |
| S-1 denied the CALLER | `status: error`, `refusal_kind: "trust"`, the refusal and nothing else | A caller not permitted to invoke the agent must not be told what it is for |
| S-3 blocked the agent's OWN output | unchanged — `status: error` | The agent produced something its output gate would not pass. The sender can do nothing with that, and must not be invited to retry |
| The agent genuinely broke | unchanged — `status: error` | The one signal that says this is an operations problem |

Nothing downstream reads `status` to detect a refusal any more: the envelope names the
refusal in `refusal_kind`. A contract that could only be read by the symptom it was fixing
was not a contract.

Enforced by `tests/unit/test_disclaimer_always_present.py` —
`test_a_refused_MESSAGE_is_delivered_and_says_what_to_change`,
`test_a_REAL_failure_is_still_an_error` (its control), and
`test_the_gate_token_is_matched_as_a_whole_token`.
