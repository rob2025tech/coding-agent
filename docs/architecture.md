# Coding Agent — Architecture v0.1

## 1. Purpose

Build a provider-neutral, terminal-first coding-agent runtime inspired by the general capabilities of Claude Code, Qoder CLI, Cursor Agent, and Codex.

The goal is **not** to reproduce any proprietary implementation.

The goal is to build an independent agent runtime that can:

1. Understand a software repository.
2. Inspect files and search code.
3. Plan a requested change.
4. Request tools.
5. Enforce permissions before side effects.
6. Modify files.
7. Run tests or other verification commands.
8. Observe results.
9. Diagnose failures.
10. Iterate until the task is complete or the agent must stop.
11. Explain what it did.

The first implementation should prioritize **reliability, safety, testability, and provider independence** over features.

---

## 2. v0.1 Scope

### In scope

* Terminal/CLI interface
* Agent session state
* Iterative agent loop
* Model-provider abstraction
* Deterministic mock model provider
* Context abstraction
* Tool abstraction
* Tool-call/result protocol
* Permission gate
* Basic filesystem tools
* Repository search
* Controlled shell execution
* Basic verification loop
* Event stream
* Automated tests

### Explicitly out of scope for v0.1

* GUI
* Web application
* VS Code extension
* Desktop application
* MCP
* Subagents
* Multi-agent orchestration
* Vector database
* Long-term semantic memory
* Cloud execution
* Autonomous GitHub PR creation
* Sophisticated model routing
* Automatic model/provider selection
* Billing integration
* Voice/audio
* Browser automation

These may be added later without changing the fundamental agent/tool boundary.

---

## 3. Core Architectural Principle

The model must reason about actions, but it must **never directly execute tools**.

The fundamental execution boundary is:

```text
User
  ↓
Agent Runtime
  ↓
Context Builder
  ↓
Model Provider
  ↓
Model Response
  ↓
Tool Call
  ↓
Permission Policy
  ├── ALLOW ──→ Tool Executor ──→ Tool Result
  ├── ASK ────→ User ───────────→ Tool Executor / Deny
  └── DENY ───→ Tool Result
                         ↓
                    Agent Runtime
                         ↓
                    Model Provider
```

The runtime owns execution.

The model proposes actions.

Permissions determine whether side effects may occur.

---

## 4. Major Components

### 4.1 AgentSession

Represents one user task from start to completion.

Responsibilities:

* session identity
* user request
* working directory
* current state
* conversation/history
* tool-call history
* verification history
* cancellation/interruption
* completion status

A session must be serializable in a future version so that interrupted work can eventually be resumed.

---

### 4.2 AgentTurn

Represents one model/runtime interaction.

A turn may contain:

* context supplied to the model
* model request
* model response
* zero or more tool calls
* zero or more tool results
* final response

The system must preserve enough information to reconstruct what happened during a turn.

---

### 4.3 ModelProvider

The agent runtime must not depend directly on a specific model vendor.

Conceptually:

```text
ModelProvider
├── describe()
├── generate(request)
└── stream(request)
```

Implementations:

```text
MockModelProvider   # deterministic, offline (default)
HttpModelProvider   # real HTTP endpoint (OpenAI-compatible), stdlib transport
```

`HttpModelProvider` (v0.2, D21) adapts a single tool-capable HTTP wire format to
the provider-neutral contracts using only the standard library; the API key comes
from an environment variable and is sent only as an `Authorization` header — never
in a URL, contract type, event, or `ProviderError`. Its HTTP traffic is the **model
transport** configured by the operator, not an agent network capability, so the
shell `NETWORK`→DENY policy and arbitrary-shell DENY are unchanged.

The runtime should not contain provider-specific business logic.

---

### 4.4 Context

Context is the information supplied to the model to help it reason about the current task.

Initial context may include:

* user request
* working directory
* repository structure
* relevant files
* search results
* tool results
* previous turns
* project instructions

The initial implementation should use simple deterministic mechanisms.

Do not introduce a vector database merely to solve context management.

---

### 4.5 Tool

A tool is a capability exposed to the model.

Initial tools:

```text
list_files
read_file
search
edit_file
shell
```

Future tools may include:

```text
git
MCP
web
browser
database
```

`write_file` was adopted in v0.2 (see `docs/decisions.md` D20 and `docs/contracts.md` §41): it atomically creates a new UTF-8 file or replaces an existing one, and does not create missing parent directories. `edit_file` modifies an existing file only; it does not create files (see `docs/contracts.md` §38).

Every tool must have:

* stable name
* description
* input schema
* output/result representation
* side-effect classification
* permission requirements

The tool contracts in `docs/contracts.md` are authoritative for model-visible tools, permission checks, execution, and results.

---

### 4.6 ToolCall

A model-generated request to invoke a tool.

A ToolCall should contain at least:

```text
tool name
arguments
call ID
```

The runtime validates the ToolCall before execution.

Invalid tool calls must never reach the executor.

---

### 4.7 ToolResult

Every tool execution produces a structured result.

A result should distinguish at least:

```text
success
failure
denied
cancelled
```

The result should contain enough information for the model to understand what happened without exposing unnecessary internal implementation details.

---

### 4.8 PermissionPolicy

Every potentially side-effecting tool operation passes through a permission gate.

The initial decision model is:

```text
ALLOW
ASK
DENY
```

Example initial policy:

| Operation                | Default |
| ------------------------ | ------- |
| List files               | ALLOW   |
| Read file                | ALLOW   |
| Search repository        | ALLOW   |
| Git status *(exact argv only)* | ALLOW   |
| Git diff *(exact argv only)*   | ALLOW   |
| Run tests / test runners | ASK     |
| Modify source            | ASK     |
| Create file              | ASK     |
| Delete file              | ASK     |
| Git commit               | ASK     |
| Git push                 | DENY    |
| Arbitrary network access | DENY    |

### Shell policy (conservative, not a sandbox)

The `shell` tool is gated by a **conservative policy** — not a security
classifier and not a sandbox:

* Tokenize the command with `shlex` and match against an **exact-argv allowlist**.
* Reject (→ ASK) any command containing shell metacharacters
  (`|`, `;`, `&`, `>`, `<`, a backtick, `$( )`, or newlines).
* Execute with `shell=False`.
* ALLOW (exact argv only, no other arguments): `git status`,
  `git status --short`, `git diff`, `git diff --stat`, `git log --oneline -n 20`.
* `pytest`, `python`, and any test runner are **not** allowlisted — they execute
  repository code. They are ASK, unless they are the configured verify command
  run by the Verifier (see §4.10).
* DENY (advisory, defense in depth, **non-exhaustive**):
  * `argv[0]` in `{curl, wget, ssh, scp, nc, ncat, sudo, su}`.
  * `rm` with both recursive and force flags in any form (e.g. `rm -rf`,
    `rm -r -f`, `rm --recursive --force`). `rm -r` or `rm -f` alone is **not**
    DENY — it falls through to ASK.
  * Any argument naming `.env`, `.ssh`, `.aws`, `.netrc`, `id_rsa`, or
    `.git-credentials`.
  DENY is final and cannot be overridden by `--yes` or by the model. The list is
  a defense-in-depth advisory, not a complete classifier.
* Everything else: ASK, showing the full command.

The workspace guarantee covers **file tools** only. `shell` is gated by human
approval, **not** contained: pinning `cwd` does not prevent a command from
referencing absolute or `../` paths (see §6).

ASK decisions are resolved through an **Approver** (see `docs/contracts.md`
§37): the CLI prompts interactively; `--yes` auto-approves ASK only, never DENY.

The policy must be independent of the model provider.

The model cannot override a DENY decision.

---

### 4.9 ToolExecutor

The ToolExecutor is the only component authorized to execute tools.

Responsibilities:

* validate tool calls
* enforce execution boundaries
* execute approved operations
* capture stdout/stderr where applicable
* enforce timeouts
* return structured ToolResults
* emit execution events

Execution order (authoritative detail in `docs/contracts.md` §17):

```text
1. schema validation
2. workspace resolve (file tools)
3. policy decision
4. approval (if ASK)
5. re-validate path at execution time (guards symlink / TOCTOU changes)
6. timeout-bounded run
7. structured ToolResult
8. events
```

Validation and workspace resolution run **before** the permission decision so a
human is never asked to approve a call that was always going to fail validation.
The path is re-validated **after** approval because the filesystem may change in
between.

The model must never receive direct access to the host operating system.

---

### 4.10 Verification

Verification determines whether the requested change actually works.

The initial verification loop is:

```text
Inspect
  ↓
Plan
  ↓
Modify
  ↓
Verify
  ↓
Success?
 ├── YES → Complete
 └── NO  → Diagnose
              ↓
            Modify
              ↓
            Verify
```

Verification is **runtime-owned**. The model never supplies, alters, or triggers
the verify command.

* The verify command is **human-supplied** via the CLI flag `--verify "<cmd>"`
  only (v0.1). Its timeout is set with `--verify-timeout <seconds>` (default
  120s). The command is a **single argv**: it is run with `shlex.split` and
  `shell=False`, so shell syntax (pipes, redirection, `&&`) requires a wrapper
  script invoked as the verify command.
* A model-requested `shell` call **never** counts as verification, even if its
  command string matches the verify command.
* Because the human configured it, the verify command is **pre-authorized** (no
  ASK). It runs via the Verifier directly — not through the model's tool-call
  path — with argv from `shlex.split`, `shell=False`, `cwd=repo_root`, and the
  `--verify-timeout` bound.
* **Trigger:** when the model signals completion and any **write** has occurred
  since the last passing verification, the runtime runs the verifier. On failure,
  the result is appended to context and the loop continues (bounded by limits).
* **Staleness:** any write after a passing verification invalidates it.
* A **write** means a successful `edit_file` **or** any `shell` execution that is
  not on the read-only allowlist (§4.8) — a non-allowlisted shell call is treated
  as a potential write even if it changes nothing.
* **No verify command configured:** if no writes occurred → COMPLETED; if writes
  occurred → COMPLETED_UNVERIFIED.

The `VerificationResult` contract is in `docs/contracts.md` §35.

The agent must not assume that a successful file edit means the task succeeded.

---

### 4.11 Event Stream

Important runtime events should be represented explicitly.

Initial events:

```text
SessionStarted
ModelRequested
ModelResponded
ToolRequested
PermissionRequested
PermissionGranted
PermissionDenied
ToolStarted
ToolCompleted
ToolFailed
VerificationStarted
VerificationPassed
VerificationFailed
SessionInterrupted
SessionCompleted
SessionFailed
```

The event stream should support:

* human-readable CLI output
* debugging
* testing
* future JSON/JSONL logging
* future UI integration
* future session replay

---

## 5. Agent State Machine

The initial state machine is:

```text
CREATED
  ↓
RUNNING
  ↓
MODEL_THINKING
  ↓
TOOL_REQUESTED
  ↓
PERMISSION_CHECK
  ├── ALLOW → EXECUTING_TOOL
  ├── ASK   → WAITING_FOR_USER
  └── DENY  → TOOL_DENIED
                  ↓
             TOOL_RESULT
                  ↓
             MODEL_THINKING

EXECUTING_TOOL
  ↓
TOOL_RESULT
  ↓
MODEL_THINKING

MODEL_THINKING
  ├── final response → VERIFICATION_CHECK
  └── tool call      → TOOL_REQUESTED

VERIFICATION_CHECK
  ├── verifier passed → COMPLETED
  ├── verifier failed → MODEL_THINKING (diagnose; bounded by limits)
  ├── no verify command & no writes → COMPLETED
  └── no verify command & writes occurred → COMPLETED_UNVERIFIED

Any active state
  ├── interruption → INTERRUPTED
  └── unrecoverable error → FAILED
```

Terminal states: `COMPLETED`, `COMPLETED_UNVERIFIED`, `INTERRUPTED`, `FAILED`.

In `VERIFICATION_CHECK`, a **write** means a successful `edit_file` or any
non-allowlisted `shell` execution (see §4.10); either invalidates a prior passing
verification.

The implementation should prevent invalid state transitions.

---

## 6. Repository Boundary

The agent operates relative to an explicit working directory.

The runtime should know:

```text
working directory
repository root
```

Tools must not silently operate outside the permitted workspace.

Path traversal and unsafe filesystem access must be validated before execution.

The initial implementation should prefer explicit workspace boundaries over unrestricted host access.

The workspace guarantee applies to **file tools** (`list_files`, `read_file`, `edit_file`, `search`). The `shell` tool is **not** contained by the workspace: it is gated by human approval (see §4.8), and pinning `cwd` to the repository root does not prevent a command from referencing absolute or `../` paths.

---

## 7. Provider Independence

The agent runtime must be independent of any single model vendor.

This means:

```text
Agent Runtime
      ↓
ModelProvider interface
      ↓
┌──────────────┬──────────────┬──────────────┐
│ Mock         │ Provider A   │ Provider B   │
└──────────────┴──────────────┴──────────────┘
```

The first real provider should be implemented only after the complete mock-based agent loop works.

Provider routing, cost optimization, quota management, and local-model selection are future capabilities.

The provider-neutral contracts in `docs/contracts.md` are authoritative for communication between the runtime and model providers; provider SDK types must not cross into the core runtime.

---

## 8. Testing Strategy

The agent must be testable without making external AI calls.

### Unit tests

Test independently:

* session state
* state transitions
* tool schemas
* permission decisions
* path validation
* tool execution
* event generation
* provider interface
* verification results

### Deterministic agent-loop tests

Use `MockModelProvider` to simulate:

```text
model → tool call → tool result → model → final answer
```

The mock provider must be able to script both of these **named scenarios**:

* **Happy path:** read → edit (approved) → completion → runtime verification passes.
* **Failure path:** verification fails → diagnose → edit → verification passes.

### Integration tests

Use a temporary fixture repository.

Example:

```text
fixture/
├── README.md
└── hello.py
```

A test should verify that the agent can:

1. inspect the repository
2. read the relevant file
3. request an edit
4. pass the permission gate
5. modify the file
6. pass runtime verification
7. observe the result
8. finish successfully

### Safety tests

Explicitly test that:

* DENY cannot be overridden by the model
* unauthorized paths cannot be accessed
* destructive commands require appropriate permission
* malformed tool calls are rejected
* cancelled operations do not continue executing

---

## 9. Design Constraints

### Prefer simple interfaces

Do not create abstractions until they have a clear responsibility.

### Keep model reasoning separate from execution

The model proposes.

The runtime decides.

The executor performs.

### Prefer deterministic behavior

Especially for:

* permissions
* filesystem boundaries
* state transitions
* tool validation
* verification

### Avoid premature infrastructure

Do not introduce:

* databases
* vector stores
* distributed queues
* microservices
* Kubernetes
* elaborate event buses

unless a demonstrated requirement justifies them.

### Make future expansion possible

The architecture should eventually support:

```text
CLI
Web UI
VS Code
Desktop
API
```

without requiring the agent runtime to be rewritten.

---

## 10. Future Architecture

The long-term architecture may evolve toward:

```text
                    ┌───────────────────┐
                    │   User Interfaces │
                    │ CLI / Web / IDE   │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │   Agent Runtime   │
                    └─────────┬─────────┘
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
          Context           Tools          Permissions
             │                │                │
             │        ┌───────┼────────┐       │
             │        ▼       ▼        ▼       │
             │      Files   Shell     Git      │
             │                         │
             │                        MCP
             │
             ▼
       Model Provider
             │
       ┌─────┼───────────────┐
       ▼     ▼               ▼
     Cloud  Cloud          Local
     Model  Model          Model
             │
             ▼
        Future Model
        Resource Router
```

The long-term system may also support:

```text
Subagents
Skills
MCP
Model routing
Cost tracking
Quota tracking
Session persistence
Context compaction
Background execution
Cloud sandboxes
```

These are intentionally deferred from v0.1.

---

## 11. v0.1 Success Criterion

The first milestone is successful when the agent can perform the following workflow reliably:

```text
User:
"Change hello.py so it prints Hello, Rob."

        ↓

Agent understands task

        ↓

Agent inspects repository

        ↓

Agent reads hello.py

        ↓

Agent proposes an edit_file modification

        ↓

Permission gate asks for approval

        ↓

Approved tool executes

        ↓

Runtime runs the human-configured verification

        ↓

Agent observes result

        ↓

If necessary:
    diagnose → modify → verify

        ↓

Agent reports completion
```

If this workflow works with a deterministic mock model and a real model provider, the core v0.1 architecture is validated.

---

## 12. Architectural North Star

The project is not primarily an AI chatbot.

It is an **agent runtime for software engineering**.

The durable abstraction is:

```text
Model
  +
Context
  +
Tools
  +
Permissions
  +
Execution
  +
Verification
  +
State
```

The model is replaceable.

The UI is replaceable.

The tools are extensible.

The runtime is the product.

---

## 13. Implementation Language & Module Layout (Provisional)

### Language

* Python 3.11+.
* Stdlib-only core; no vendor SDK types in the core runtime.
* `async` **only** at the provider and executor boundaries. Pure logic —
  context building, policy checks, workspace resolution — stays synchronous.

### Provisional module layout

This tree is a **guideline**; implementers may merge trivial modules:

```text
src/coding_agent/
├── cli.py
├── runtime.py
├── session.py
├── context.py
├── permissions.py
├── executor.py
├── workspace.py
├── verification.py
├── events.py
├── errors.py
├── contracts.py
├── providers/
│   ├── base.py
│   └── mock.py
└── tools/
    ├── base.py
    ├── registry.py
    ├── filesystem.py
    ├── search.py
    └── shell.py
```

The **stable boundaries** listed in `docs/contracts.md` §29 — not the file tree
— are the contract.
