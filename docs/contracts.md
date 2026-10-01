# Coding Agent — Tool & Provider Data Contracts

## 1. Contract Goals

These contracts define the stable boundaries between:

```text
Agent Runtime
    ↕
Model Provider
    ↕
Tool Calls
    ↕
Permission Policy
    ↕
Tool Executor
```

The contracts must be:

* provider-neutral
* deterministic where possible
* serializable
* testable without external AI calls
* explicit about failures
* extensible without breaking existing tools
* independent of any particular model vendor or SDK

The model provider must never directly execute tools.

The runtime is authoritative over tool execution and permissions.

---

# 2. Common Identifiers

All runtime objects should use opaque string identifiers.

Recommended format:

```text
session_id
turn_id
message_id
tool_call_id
event_id
```

The implementation may use UUIDs internally.

Identifiers must be unique within their applicable scope.

Do not expose database-specific IDs as public contracts.

---

# 3. Model Provider Contract

## 3.1 ModelProvider

Conceptual interface:

```text
ModelProvider
├── describe()
├── generate(request)
└── stream(request)
```

The runtime should depend only on this interface.

It must not import provider-specific SDK types into the core agent runtime.

---

# 4. ModelDescriptor

A provider exposes metadata about a model through:

```json
{
  "provider_id": "string",
  "model_id": "string",
  "display_name": "string",
  "capabilities": {
    "text": true,
    "tool_calling": true,
    "streaming": true,
    "vision": false,
    "reasoning": true
  },
  "context_window": 0,
  "max_output_tokens": 0
}
```

### Required fields

```text
provider_id
model_id
capabilities
context_window
```

### Optional fields

```text
display_name
max_output_tokens
pricing
limits
metadata
```

The core runtime must not require pricing information.

Pricing belongs to the future resource/cost-control layer.

---

# 5. ModelCapabilities

Capabilities should be explicit rather than inferred from provider names.

Initial capability set:

```json
{
  "text": true,
  "tool_calling": true,
  "streaming": true,
  "vision": false,
  "reasoning": false
}
```

Future capabilities may include:

```text
audio_input
audio_output
structured_output
parallel_tool_calls
computer_use
extended_context
```

The runtime should reject a task requiring an unavailable capability rather than silently degrading behavior.

---

# 6. ModelRequest

A model request contains everything the provider needs to produce the next response.

Conceptual schema:

```json
{
  "request_id": "req_123",
  "session_id": "session_123",
  "turn_id": "turn_123",
  "model": {
    "provider_id": "mock",
    "model_id": "mock-v1"
  },
  "system_instructions": "string",
  "messages": [],
  "tools": [],
  "options": {
    "temperature": null,
    "max_output_tokens": null
  },
  "metadata": {}
}
```

The runtime owns the request ID.

The provider must not create a second identity for the same request.

---

# 7. ModelMessage

Messages should use a provider-neutral representation.

Initial message roles:

```text
system
user
assistant
tool
```

Conceptual representation:

```json
{
  "message_id": "msg_123",
  "role": "assistant",
  "content": [],
  "tool_calls": []
}
```

---

# 8. MessageContent

Content should be represented as typed parts rather than requiring a single string.

Initial content types:

```text
text
```

Future content types:

```text
image
audio
document
```

Example:

```json
{
  "type": "text",
  "text": "Please inspect the authentication implementation."
}
```

This allows multimodal providers to be added later without changing the fundamental message contract.

---

# 9. Tool Definition Contract

A tool exposed to the model has:

```json
{
  "name": "read_file",
  "description": "Read a UTF-8 text file within the workspace.",
  "input_schema": {},
  "side_effect": "none",
  "permission": "read"
}
```

Required fields:

```text
name
description
input_schema
side_effect
permission
```

---

# 10. Tool Name

Tool names must:

* be unique within a session
* be stable
* use a provider-neutral naming convention
* not encode implementation details

Examples:

```text
list_files
read_file
search
shell
```

Avoid:

```text
anthropic_read_file
openai_shell
qoder_search
```

The runtime owns the tool namespace.

---

# 11. Tool Input Schema

Tool inputs must be schema-defined.

Example:

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string"
    }
  },
  "required": ["path"],
  "additionalProperties": false
}
```

The runtime must validate tool arguments before execution.

A model-generated tool call with invalid arguments must produce a structured failure and must not execute.

---

# 12. Tool Side Effects

Every tool declares its side-effect classification.

Initial values:

```text
none
filesystem_write
process_execution
network
destructive
```

Examples:

| Tool          | Side effect         |
| ------------- | ------------------- |
| `list_files`  | `none`              |
| `read_file`   | `none`              |
| `search`      | `none`              |
| `shell`       | `process_execution` |
| `edit_file`   | `filesystem_write`  |
| `delete_file` | `destructive`       |

This classification is advisory metadata.

The permission policy remains authoritative.

---

# 13. Tool Permission Classes

Initial permission classes:

```text
read
write
execute
network
destructive
```

Example:

```json
{
  "name": "shell",
  "permission": "execute"
}
```

A future policy can therefore make decisions based on capability rather than hard-coding individual tool names.

---

# 14. ToolCall

A model-generated tool request:

```json
{
  "tool_call_id": "call_123",
  "name": "read_file",
  "arguments": {
    "path": "src/app.ts"
  }
}
```

Required:

```text
tool_call_id
name
arguments
```

The runtime must treat the model's arguments as untrusted input.

---

# 15. PermissionRequest

Before executing a tool requiring approval:

```json
{
  "permission_request_id": "perm_123",
  "tool_call_id": "call_123",
  "tool_name": "edit_file",
  "permission_class": "write",
  "reason": "Modify src/app.ts to implement the requested change.",
  "risk": "medium"
}
```

Initial risk levels:

```text
low
medium
high
```

Risk is informational.

The policy determines the actual decision.

---

# 16. PermissionDecision

The permission system returns:

```json
{
  "decision": "allow"
}
```

or:

```json
{
  "decision": "ask",
  "reason": "This operation modifies source files."
}
```

or:

```json
{
  "decision": "deny",
  "reason": "Network access is disabled."
}
```

Allowed decisions:

```text
allow
ask
deny
```

The model cannot convert `deny` into `allow`.

---

# 17. ToolExecutionRequest

Once authorized, the runtime creates an execution request.

```json
{
  "execution_id": "exec_123",
  "tool_call_id": "call_123",
  "tool_name": "read_file",
  "arguments": {
    "path": "src/app.ts"
  },
  "workspace": "/project",
  "timeout_ms": 10000
}
```

The executor must validate the workspace and arguments again.

Authorization is not a substitute for execution-time validation.

---

# 18. ToolResult

Every tool produces:

```json
{
  "tool_call_id": "call_123",
  "status": "success",
  "output": {},
  "error": null,
  "metadata": {}
}
```

Status values:

```text
success
failure
denied
cancelled
timeout
```

---

# 19. ToolResult Output

Tool output should be structured whenever practical.

Example:

```json
{
  "status": "success",
  "output": {
    "path": "src/app.ts",
    "content": "..."
  }
}
```

For shell:

```json
{
  "status": "success",
  "output": {
    "stdout": "All tests passed.",
    "stderr": "",
    "exit_code": 0
  }
}
```

Do not force all tools into string-only output.

The model adapter may serialize structured results into provider-specific formats.

---

# 20. Tool Error Contract

Errors should be structured:

```json
{
  "code": "PATH_OUTSIDE_WORKSPACE",
  "message": "The requested path is outside the permitted workspace.",
  "retryable": false,
  "details": {}
}
```

Initial error codes:

```text
INVALID_ARGUMENTS
TOOL_NOT_FOUND
PERMISSION_DENIED
PATH_OUTSIDE_WORKSPACE
EXECUTION_FAILED
TIMEOUT
CANCELLED
INTERNAL_ERROR
```

The model should receive enough information to recover when recovery is possible.

---

# 21. ModelResponse

The provider returns a normalized response.

```json
{
  "response_id": "resp_123",
  "stop_reason": "tool_call",
  "content": [],
  "tool_calls": [],
  "usage": null,
  "metadata": {}
}
```

Initial stop reasons:

```text
tool_call
completed
length
error
cancelled
```

---

# 22. Tool-Calling Response

Example:

```json
{
  "response_id": "resp_123",
  "stop_reason": "tool_call",
  "content": [],
  "tool_calls": [
    {
      "tool_call_id": "call_123",
      "name": "read_file",
      "arguments": {
        "path": "src/app.ts"
      }
    }
  ]
}
```

The runtime converts this into a ToolCall.

The provider's native tool-call object must not leak into the core runtime.

---

# 23. Final Model Response

When the model believes the task is complete:

```json
{
  "response_id": "resp_124",
  "stop_reason": "completed",
  "content": [
    {
      "type": "text",
      "text": "Implemented the requested change and verified the tests."
    }
  ],
  "tool_calls": []
}
```

The runtime then transitions the session to `COMPLETED`.

The runtime, not the model, ultimately determines whether required verification has occurred.

---

# 24. Usage Contract

Providers may report usage:

```json
{
  "input_tokens": 1200,
  "output_tokens": 450,
  "cached_input_tokens": 0
}
```

Usage is optional in v0.1.

The runtime should preserve it when available.

Do not make the core agent loop dependent on provider-specific token accounting.

Future cost tracking can consume this information.

---

# 25. Provider Error Contract

Provider errors must be normalized.

```json
{
  "code": "RATE_LIMITED",
  "message": "Provider rate limit reached.",
  "retryable": true,
  "retry_after_ms": 5000,
  "provider_id": "example-provider",
  "metadata": {}
}
```

Initial normalized codes:

```text
AUTHENTICATION_FAILED
INVALID_REQUEST
RATE_LIMITED
CONTEXT_TOO_LARGE
MODEL_UNAVAILABLE
NETWORK_ERROR
PROVIDER_ERROR
CANCELLED
```

Provider-specific error information may be retained in `metadata`.

The core runtime must not depend on provider-specific error classes.

---

# 26. Streaming Contract

Streaming is optional for the first mock implementation but should be supported by the interface.

Conceptually:

```text
ModelStreamEvent
```

with event types such as:

```text
text_delta
tool_call_delta
usage_update
completed
error
```

The runtime should be able to consume a stream and reconstruct a normalized `ModelResponse`.

Do not make the agent state machine depend on token-by-token streaming.

---

# 27. Agent Tool Loop Contract

The runtime loop is conceptually:

```text
while session is active:

    context = ContextBuilder.build(session)

    response = ModelProvider.generate(request)

    if response contains final answer:
        verify completion requirements
        complete session

    if response contains tool calls:

        for each tool call:

            validate tool call

            permission = PermissionPolicy.check(tool call)

            if permission == ASK:
                ask user

            if permission == DENY:
                append denied ToolResult
                continue

            execute tool

            append ToolResult

            emit events

        continue model loop
```

The runtime must impose configurable limits on:

```text
maximum turns
maximum tool calls
maximum execution time
maximum repeated failures
```

These limits prevent runaway loops.

---

# 28. Example End-to-End Contract

User request:

```text
"Change hello.py so it prints Hello, Rob."
```

Model response:

```json
{
  "tool_calls": [
    {
      "tool_call_id": "call_001",
      "name": "read_file",
      "arguments": {
        "path": "hello.py"
      }
    }
  ]
}
```

Permission:

```json
{
  "decision": "allow"
}
```

Tool result:

```json
{
  "tool_call_id": "call_001",
  "status": "success",
  "output": {
    "path": "hello.py",
    "content": "print('Hello')"
  }
}
```

Model then requests:

```json
{
  "tool_calls": [
    {
      "tool_call_id": "call_002",
      "name": "edit_file",
      "arguments": {
        "path": "hello.py",
        "operation": "replace",
        "old_text": "print('Hello')",
        "new_text": "print('Hello, Rob.')"
      }
    }
  ]
}
```

Permission:

```json
{
  "decision": "ask"
}
```

User approves.

Tool executes.

Then the model requests:

```json
{
  "tool_calls": [
    {
      "tool_call_id": "call_003",
      "name": "shell",
      "arguments": {
        "command": "python hello.py"
      }
    }
  ]
}
```

The tool returns:

```json
{
  "tool_call_id": "call_003",
  "status": "success",
  "output": {
    "stdout": "Hello, Rob.\n",
    "stderr": "",
    "exit_code": 0
  }
}
```

The model returns a final response.

The runtime marks the session completed.

---

# 29. Contract Stability Rules

The following are considered stable architectural boundaries:

```text
ModelProvider
ModelRequest
ModelResponse
ToolDefinition
ToolCall
ToolResult
PermissionDecision
AgentSession
AgentTurn
```

Provider-specific SDK objects must remain outside these boundaries.

Tool implementations may evolve independently as long as their public contracts remain compatible.

Adding optional fields should generally be backward compatible.

Changing the meaning of an existing field requires an explicit architecture decision.

---

# 30. Deferred Extensions

Future contracts may add:

```text
MCPTool
Subagent
Skill
BrowserTool
ComputerUseTool
ImageContent
AudioContent
SessionPersistence
ModelRouter
ResourcePolicy
CostRecord
QuotaRecord
CloudSandbox
```

These should be introduced only when a concrete requirement exists.

The v0.1 contracts should remain small enough that the entire system can be understood by one developer.
