# 02 · Design Specification — CMN-C1-613 Enterprise Zendesk Macro Apply Agent

## Position in AgentCore Architecture

- **Agent Class**: `ZendeskMacroApplyAgent` (graph class `Graph` in `src/graph/graph.py`)
- **L1 Base**: `AgentBaseGraph` (L1 direct). ToolCalling is a conceptual reference
  only — not an inheritance target (2026-05-18 L2-abolition policy). No Level-0
  (`agenticstar`) import; no `AutonomousBaseGraph` (this is a deterministic pipeline,
  not an LLM think-act loop).
- **Three-Layer Separation**:
  - **State**: flat `TypedDict` extending `framework.schemas.agent_state.AgentState`;
    all compound values JSON-serialized to `str` (msgpack-safe checkpoints).
  - **Node**: `FunctionNode` subclasses; override only `execute(self, state) -> dict`
    (no `config` param — the wheel's `BaseNode.__call__(self, state)` calls
    `execute(state)`). Context via `InvocationContext.from_state(state)` when needed.
  - **Graph**: composition — `register_nodes()` + `add_edges()` on `AgentBaseGraph`.

## Capability

A single Cat 1 capability: **apply one existing Zendesk macro to one ticket from a
natural-language instruction**, behind a name-resolution → disambiguation →
applicability/high-impact → confirm → S-1-gated write → S-3 egress → S-4 audit
envelope. Deterministic (no LLM); the macro-name match is a rule-based resolution over
the account's macro list.

## Architecture Overview

### Node Configuration

The five logical steps (IntentParse → MacroList → MacroSelect → MacroApply →
ConfirmationGenerate) map onto the `AgentBaseGraph` fold. The two read/resolve stages
are registered as extra nodes between `pre_process` and the `main` writer slot, so the
resolution is unit-testable independently of the write.

| Node (slot) | Class | Responsibility | Reads | Writes |
|---|---|---|---|---|
| initialize | InitializeNode (default) | framework seed | — | framework fields |
| pre_process | `PreProcessNode` | IntentParse + S-2 input gate; language detect | `user_input` | `parsed_intent`, `target_context`, `validation_error`, `output_language` |
| macro_list | `MacroListNode` | `GET /api/v2/macros` (search/active) | `target_context` | `macro_candidates` |
| macro_select | `MacroSelectNode` | name→ID resolution, disambiguation, applicability + high-impact flag | `parsed_intent`, `macro_candidates` | `selected_macro`, `disambiguation`, `validation_error` |
| main | `MacroApplyNode` | S-1-gated apply; confirm-gate + dry-run guard | `target_context`, `selected_macro` | `apply_result` |
| post_process | `PostProcessNode` | render EN/JA/bilingual confirmation; S-3 egress redaction | all | `confirmation_report`, `formatted_output` |
| finalize | FinalizeNode (default) | framework close | — | `status` |

### Data Flow

```
START → initialize → pre_process → macro_list → macro_select → main → {route}
      → post_process → finalize → END
```

`add_edges()` is overridden to insert `macro_list` and `macro_select` between
`pre_process` and `main`, preserving the standard `{route}` tail after `main`
(`add_conditional_edges("main", self.route)`; `post_process → finalize → END`).

### State Definition

Flat `TypedDict` (`src/schemas/state.py`), all compound fields JSON strings.

| Field | Type | Purpose |
|---|---|---|
| `parsed_intent` | `str` (JSON) | `{ticket_id, macro_name, confirm, dry_run}` |
| `target_context` | `str` (JSON) | resolved target `{ticket_id, subdomain?}` + flags |
| `validation_error` | `str` | non-empty → downstream short-circuit + refusal report |
| `output_language` | `str` | `en` / `ja` / `bilingual` (default `en`) |
| `macro_candidates` | `str` (JSON list) | `[{id, title, active, has_public_comment}]` |
| `selected_macro` | `str` (JSON) | resolved `{id, title, has_public_comment}` |
| `disambiguation` | `str` (JSON list) | candidate titles when >1 match (never guess) |
| `apply_result` | `str` (JSON) | `{applied, dry_run, needs_confirmation, ticket_id, macro_id, macro_title}` |
| `confirmation_report` | `str` | final Markdown report |
| `formatted_output` | `str` | same report; surfaced by `get_output()` to `invoke()` |

**State Constraints (mandatory):** flat TypedDict; primitives + JSON-string only; no
JWT/API keys/credentials; no Pydantic/dataclass/arbitrary objects; `InvocationContext`
never in State (accessed via `config["configurable"]` / `InvocationContext.from_state`).
The `ZENDESK_TOKEN` is fetched at call time via `current_secrets().require()` and never
persisted to State or the checkpoint.

## Zendesk API Contract

- **List:** `GET https://{subdomain}.zendesk.com/api/v2/macros?active=true` → macros
  (`id`, `title`, `active`, `actions`). `has_public_comment` is derived from a macro
  action of field `comment_mode_is_public` / a public `comment` action.
- **Apply (two-call, persisting):** `GET https://{subdomain}.zendesk.com/api/v2/tickets/{id}/macros/{macro_id}/apply.json` returns the resulting ticket changes as a *preview* (does not persist), then `PUT https://{subdomain}.zendesk.com/api/v2/tickets/{id}.json` with `{"ticket": <returned changes>}` persists them. A POST to the apply path is a non-persisting preview and is not used.
  (Zendesk returns the resulting ticket + comment; the agent reports the applied macro,
  not raw ticket PII.)
- Auth: `Authorization: Bearer {ZENDESK_TOKEN}` (or API-token scheme), per-call from the
  bound secret provider; token never cached on the client or in State.

## 5-Layer Security Mapping

| Layer | Where | Implementation |
|---|---|---|
| **S-1** Trust gate | every node (`required_trust_level: ClassVar[TrustLevel] = VERIFIED_EXTERNAL`) | the write (`MacroApplyNode`) requires `VERIFIED_EXTERNAL`; the framework `BaseNode.__call__` refuses an under-trusted caller and returns an ERROR state (does not raise). |
| **S-2** Input gate | `PreProcessNode._extra_security_gate_input(state)` | rejects oversized / path-traversal / control-char input → `status = ERROR` (no raise). Framework `@final _security_gate_input` (PII scan) runs first automatically. |
| **S-3** Output gate | `PostProcessNode._extra_security_gate_output(result)` | redacts any `ZENDESK_TOKEN` shape from `confirmation_report` / `formatted_output`. The report carries macro name + ticket ID + outcome, never raw customer PII or credentials. **Graph-class gates are dead on this wheel** (`AgentBaseGraph._extra_security_gate_output` absent) → S-3 lives on the post_process node. |
| **S-4** Audit | every `execute()` via `emit_trace_event(...)` | domain events only (`scope_parsed`, `macros_listed`, `macro_resolved`, `macro_disambiguation`, `macro_applied`, `macro_apply_skipped`, `report_compiled`); never `node_start/complete/error` (framework owns those). Payloads = counts/ids/flags, no PII. |
| **S-5** Credential/supply chain | `ZendeskClient` + `pyproject.toml` | secret only via `current_secrets().require("ZENDESK_TOKEN")` (never `os.environ`/State); egress guard restricts the host to `*.zendesk.com`; deps exact-pinned; `agent.yaml requires.secrets: [ZENDESK_TOKEN]`. |

## Edge Cases

- **Ambiguous macro name** (>1 match) → `disambiguation` list; no apply; report asks the
  caller to pick. Never guesses.
- **High-impact (public-reply) macro** → requires explicit `confirm` in the instruction;
  otherwise `apply_result.needs_confirmation = true`, no write, report previews the macro.
- **Macro not found** (0 matches) → `validation_error = "macro not found"`; refusal report.
- **Malformed / empty input** → `validation_error` (SUCCESS + refusal), or S-2 hard reject
  (ERROR) for unsafe input.
- **Dry-run** (explicit directive only, not a bare "preview") → projected apply, no write.
- **Zendesk API error** → captured as `validation_error` + `apply_result.applied=false`;
  no partial state; report explains the failure.
- **No fabrication** — only macros that exist in the account are applied.

## Framework Utilization

- `InvocationContext` (via `config["configurable"]` / `from_state`) — trust level, secrets.
- `emit_trace_event` from `shared.utils.audit_logger` (wrapped nowhere else).
- S-2/S-3 via `_extra_security_gate_input()` / `_extra_security_gate_output()` — never
  override the `@final` `_security_gate_input()` / `_security_gate_output()` (TypeError at
  class definition).
- HITL: `hitl.enabled` = false, `memory_enabled` = false (single-shot operation; the
  confirmation gate is a state flag, not a backbone interrupt).

## Composition Pattern

- **Pattern**: Standalone (single-graph, no GraphNode / RemoteAgentNode).
- **Error propagation**: in-pipeline short-circuit — a gate refusal or upstream error is a
  valid business outcome carried in `validation_error` / `apply_result` (status SUCCESS),
  rendered by `post_process`; a genuinely unsafe input or trust denial is an ERROR state.

## Import Isolation Confirmation

- Template does not import the Level-0 `agenticstar` SDK.
- Imports target `framework.*` and `shared.*` only (plus stdlib + `requests`). No
 `framework.security` / `framework.llm` (stub-harness-only, absent on the wheel).

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|---|---|---|---|---|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **AgentBaseGraph** | deterministic 5-step pipeline; no autonomous loop |
| Read/resolve placement | fold into `main` | separate `macro_list`/`macro_select` nodes | **separate nodes** | resolution unit-testable independently of the write; mirrors released write-agent (internal reference removed) |
| Secret access | `os.environ` | `current_secrets().require()` | **current_secrets** | namespace isolation; never persisted to State/checkpoint |
| S-3 gate location | graph class | post_process node hook | **post_process node** | graph-class gate is dead on the wheel (`_extra_security_gate_output` absent) |
| Macro resolution | LLM | rule-based match over the macro list | **rule-based** | deterministic, testable, no LLM dependency for Cat 1 |
