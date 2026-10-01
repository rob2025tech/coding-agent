# Architecture Decision Records (v0.1)

Short ADRs reconciling `docs/architecture.md` and `docs/contracts.md` with the
authoritative decisions D1–D19. New contracts were **appended** to
`docs/contracts.md` as §31–§40 (rather than inserted) to keep diffs minimal and
avoid renumbering stable sections.

---

## D1. Implementation language & async boundaries

**Decision.** Python 3.11+, stdlib-only core. `async` only at the provider and
executor boundaries; pure logic (context building, policy checks, workspace
resolution) stays synchronous. No vendor SDK types in core.

**Rationale.** Keeps the core deterministic and testable without I/O; confines
concurrency and third-party dependencies to the edges.

**Consequences.** Recorded in architecture §13. Providers/executors are awaitable;
ContextBuilder, PermissionPolicy, and Workspace are plain synchronous functions.

---

## D2. v0.1 tool set

**Decision.** v0.1 tools: `list_files`, `read_file`, `search`, `edit_file`,
`shell`. `write_file` is deferred (add only if implementation proves a need).

**Rationale.** `edit_file` (exact, unique, in-place replacement) covers the
hello.py milestone without a separate create-file path.

**Consequences.** `edit_file` moved out of "future tools" in architecture §4.5;
§4.5, the §11 success criterion, and contracts §28 are now consistent.
`WriteFileTool` added to the contracts §30 deferred list.

---

## D3. edit_file contract

**Decision.** Args `{path, old_text, new_text}`. File must exist (no creation).
`old_text` non-empty and must match exactly once (zero → `EDIT_NO_MATCH`,
multiple → `EDIT_AMBIGUOUS`). UTF-8 only (binary/undecodable → error). Preserve
line endings and trailing-newline state. Atomic write (temp + rename). Result
output includes `path` and a short unified diff.

**Rationale.** Exact-unique matching is deterministic and safe; atomic writes
avoid partial files.

**Consequences.** ToolDefinition + input_schema added in contracts §38; error
codes added in contracts §20.

---

## D4. Runtime-owned verification

**Decision.** The verify command is human-supplied (`--verify "<cmd>"` only in
v0.1 — see D14); the model never supplies or alters it. A model-requested
`shell` call never counts as verification. The command is pre-authorized (no ASK)
and runs via the Verifier directly (`shlex.split`, `shell=False`, `cwd=repo_root`,
timeout). Trigger: on completion signal when a write occurred since the
last pass. Staleness: any write after a pass invalidates it (a write includes a
non-allowlisted `shell` call — see D12). No verify
command: no writes → `COMPLETED`; writes → `COMPLETED_UNVERIFIED`. Terminal states:
`COMPLETED`, `COMPLETED_UNVERIFIED`, `INTERRUPTED`, `FAILED`.

**Rationale.** Verification must be trustworthy evidence, not a model assertion.

**Consequences.** Architecture §4.10, §5 (new `VERIFICATION_CHECK`), §8, §11
updated; contracts §23/§27/§28 updated; `VerificationResult` added in §35;
`SessionState` in §31.

---

## D5. Shell policy (conservative, not a sandbox)

**Decision.** Tokenize with `shlex`; match an exact-argv allowlist; reject (→ ASK)
any command with shell metacharacters (`| ; & > < ` `` ` `` ` $( )` or newlines);
execute with `shell=False`. ALLOW exact-argv only: `git status`, `git status
--short`, `git diff`, `git diff --stat`, `git log --oneline -n 20`. `pytest`,
`python`, and any test runner are ASK (unless they are the configured verify
command run by the Verifier, D4). DENY (advisory, defense in depth): network tools
(`curl`, `wget`, `ssh`, `scp`, `nc`), `sudo`, recursive force-delete, credential-file
access (full list: see D16) — final, not overridable by `--yes` or the model. Everything else: ASK,
showing the full command.

**Rationale.** A small exact-argv allowlist is auditable; DENY adds depth without
claiming containment.

**Consequences.** Architecture §4.8 "Run tests" changed ALLOW → ASK and a Shell
policy subsection added; §6 states the workspace guarantee covers **file tools**
only and that `shell` is gated, not contained.

---

## D6. Executor order

**Decision.** schema validation → workspace resolve (file tools) → policy decision
→ approval (if ASK) → re-validate path at execution time (symlink/TOCTOU) →
timeout-bounded run → structured ToolResult → events.

**Rationale.** Don't ask humans to approve calls that were always going to fail
validation; re-validate after approval because the filesystem may change.

**Consequences.** Architecture §4.9 and contracts §17 (ToolExecutionRequest /
executor order) updated; §27 loop pseudocode reflects the order.

---

## D7. Approver contract

**Decision.** Protocol `confirm(PermissionRequest) -> bool`. CLI prompts
interactively. `--yes` auto-approves ASK only, never DENY. Non-interactive (no TTY)
without `--yes`: ASK is treated as denied, with a clear message. All decisions emit
events.

**Rationale.** A single approval seam keeps ASK handling testable and provider-neutral.

**Consequences.** Approver added in contracts §37; referenced from architecture §4.8.

---

## D8. Contracts completed

**Decision.** Add concrete field lists for AgentSession, AgentTurn,
VerificationResult, Workspace, Approver, ToolExecutionRequest wiring, edit_file
schema, SessionState (incl. new terminals), and Limits. Add signature-level (internal,
pure) contracts for ContextBuilder.build and AgentEvent/EventType/EventSink.

**Rationale.** Stable boundaries must be explicit and serializable.

**Consequences.** Appended as contracts §31–§40. Limits defaults, the
`VERIFY_OUTPUT_MAX_CHARS` truncation value, and the verifier timeout were
subsequently filled in by D15.

---

## D9. Streaming is interface-only

**Decision.** `ModelProvider.stream()` is declared; `MockModelProvider` need not
implement it. Only `generate()` is required in v0.1.

**Rationale.** Avoids forcing streaming onto the mock while keeping the seam.

**Consequences.** Contracts §26 rewritten to state interface-only streaming.

---

## D10. Module layout is provisional

**Decision.** The proposed module tree is a guideline; implementers may merge
trivial modules. The stable boundaries listed in contracts §29 — not the file tree —
are the contract. Add Workspace and Verification to the §29 list.

**Rationale.** File layout should not ossify before implementation.

**Consequences.** Architecture §13 records the tree as provisional; contracts §29
now lists Workspace and VerificationResult.

---

## D11. Mock provider scenarios

**Decision.** The mock provider must script both a **happy path** (read →
edit[approved] → completion → runtime verification passes) and a **failure path**
(verification fails → diagnose → edit → verification passes).

**Rationale.** Both paths exercise the runtime-owned verification loop deterministically.

**Consequences.** Added as named scenarios in architecture §8 (Testing Strategy).

---

## D12. Verification staleness covers shell

**Decision.** Any `shell` execution not on the read-only allowlist (D5) counts as a
potential write and invalidates a passing verification, exactly like a successful
`edit_file`.

**Rationale.** A non-allowlisted shell call may mutate the repository; treating it
as a write keeps verification honest even when nothing visibly changed.

**Consequences.** "write" redefined across architecture §4.10/§5 and contracts
§23/§27/§35 to include non-allowlisted shell.

---

## D13. Provider interface names

**Decision.** Contracts §3.1 names are canonical: `describe()`, `generate(request)`,
`stream(request)`. Architecture §4.3 updated to match.

**Rationale.** Removes the stale `request/capabilities/model_identity` sketch and
the earlier open question; one canonical interface.

**Consequences.** Architecture §4.3 now lists describe/generate/stream.

---

## D14. Verify command source

**Decision.** v0.1 verify command comes from the CLI flag `--verify` only;
"project config file" wording removed. Timeout via `--verify-timeout` (default
120s). The command is a single argv run with `shlex.split`, `shell=False`; shell
syntax requires a wrapper script.

**Rationale.** One unambiguous, human-supplied source; no config-file parsing in
v0.1; explicit about argv vs shell.

**Consequences.** Architecture §4.10 updated; supersedes the "and/or a project
config value" wording in D4.

---

## D15. Concrete defaults (TBDs filled)

**Decision.** `VERIFY_OUTPUT_MAX_CHARS = 8000` (keep head + tail, mark elision);
verifier timeout default `120s`; Limits defaults `max_turns=20`,
`max_tool_calls=50`, `max_time_s=600`, `max_repeated_failures=3`.

**Rationale.** Removes TBDs so the runtime is implementable and tests deterministic.

**Consequences.** Contracts §34 (Limits) and §35 (VerificationResult) updated;
supersedes the "marked TBD" note in D8.

---

## D16. DENY list enumerated

**Decision.** DENY (advisory, non-exhaustive): `argv[0]` in `{curl, wget, ssh, scp,
nc, ncat, sudo, su}`; `rm` with both recursive and force flags in any form; any
argument naming `.env`, `.ssh`, `.aws`, `.netrc`, `id_rsa`, `.git-credentials`.

**Rationale.** Makes the defense-in-depth list concrete and auditable while stating
it is not a complete classifier.

**Consequences.** Architecture §4.8 DENY bullet expanded; extends the summary in D5.

---

## D17. EDIT_NOT_UTF8 confirmed

**Decision.** `EDIT_NOT_UTF8` is the error code for binary/undecodable files in
`edit_file`.

**Rationale.** Confirms the code name chosen during the D3 reconciliation.

**Consequences.** No doc change needed — contracts §20 and §38 already use
`EDIT_NOT_UTF8`. Resolves the earlier open question.

---

## D18. delete_file deferred

**Decision.** `delete_file` is added to the contracts §30 deferred list (as
`DeleteFileTool`).

**Rationale.** It appears in the §12 side-effects table but is not a v0.1 tool;
listing it as deferred removes the inconsistency.

**Consequences.** Contracts §30 now lists `DeleteFileTool`. Resolves the earlier
open question.

---

## D19. Consistency sweep

**Decision.** Grep both docs for `COMPLETED`, `VERIFICATION`, `ASK`, `ALLOW`,
`pytest` and confirm nothing contradicts D4, D5, D12.

**Rationale.** Catch residual contradictions after the amendment pass.

**Consequences.** See the final report for each reviewed hit and the changes made.

---

## Review Resolutions (R1–R5)

Resolutions from the post-D19 review of the open questions. Doc-only; no rule
changes beyond the clarifications noted. The D12 definition of “write” is
preserved unchanged.

**R1 (refines D5).** In the architecture §4.8 example policy table, `git status`
and `git diff` are annotated **exact argv only**, so the examples cannot be read
as permitting arbitrary arguments. The authoritative allowlist remains the Shell
policy subsection.

**R2 (confirms D16).** `rm` rule unchanged: `rm` with both recursive and force
flags is DENY; `rm -r` alone or `rm -f` alone is **not** DENY and falls through to
ASK. Clarified inline in architecture §4.8.

**R3 (confirms D14).** `--verify-timeout` units are **seconds**, default `120s`.
Documented in architecture §4.10 and contracts §35 (now stating “in seconds”
explicitly).

**R4 (refines D15).** Verifier-output truncation is deterministic: retain up to
**4,000 chars from the head** and **4,000 chars from the tail**, joined by
`...[truncated]...`. The rendered string is not required to be exactly 8,000
chars because the marker adds characters. Updated in contracts §35.

**R5 (confirms D16).** The credential DENY list stays as explicitly enumerated
for v0.1 (`.env`, `.ssh`, `.aws`, `.netrc`, `id_rsa`, `.git-credentials`) and is
**not** expanded to `.pem`, `id_ed25519`, `.pgpass`, etc. It remains explicitly
non-exhaustive. No change to architecture §4.8.
