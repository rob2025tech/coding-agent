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
├── request(...)
├── stream(...)
├── capabilities(...)
└── model_identity(...)
```

Initial implementations:

```text
MockModelProvider
```

followed by one or more real providers.

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
shell
```

Future tools may include:

```text
edit_file
git
MCP
web
browser
database
```

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
| Git status               | ALLOW   |
| Git diff                 | ALLOW   |
| Run tests                | ALLOW   |
| Modify source            | ASK     |
| Create file              | ASK     |
| Delete file              | ASK     |
| Git commit               | ASK     |
| Git push                 | DENY    |
| Arbitrary network access | DENY    |

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

Verification may initially use project-provided commands such as tests, linters, type checking, or a simple executable invocation.

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
  ├── final response → COMPLETED
  └── tool call      → TOOL_REQUESTED

Any active state
  ├── interruption → INTERRUPTED
  └── unrecoverable error → FAILED
```

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
6. run verification
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

Agent proposes modification

        ↓

Permission gate asks for approval

        ↓

Approved tool executes

        ↓

Agent runs verification

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
