# Experimental canonical handoff runtime

Read this reference only when developing or evaluating the repository-local handoff write path. Ordinary project/task reading and role previews do not need it.

The candidate runtime is `scripts/task_runtime.py`. It owns the canonical `HANDOFF.json`, `HANDOFF.md`, task revision, handoff events, and receiving session. It never writes `.ai-team/TASK.md`, never commits or pushes, and does not create a second role-specific envelope.

Writes are disabled unless the repository-local `.project-to-act/COLLABORATION_CONFIG.json` explicitly contains:

```json
{
  "schemaVersion": 1,
  "experimentalHandoffWrites": true
}
```

Stage 5 enables only `builder_to_verifier.verification_candidate`. The publisher must be the current actor and `STATUS.currentRole` must be `builder`; conversational behavior never assigns the role. The worktree must be clean, revision/context/code anchors must be fresh, and the payload must reference passed `self-check` evidence from the same task. Accept writes `currentRole: verifier` and starts a session containing distinct `actorId`, `role`, and `executor` fields.

Publish and accept both validate active-task `INTENT.json` ownership. Overlapping path prefixes, identical symbols/contracts, or concurrent migration ownership fail with `INTENT_CONFLICT`; the runtime does not guess which writer wins.

`handoff accept` is the receiving pull/start proof. It validates the published snapshot and starts exactly one repository-backed session. Duplicate identical publish/accept requests are no-ops. Multi-file updates use a rollback-capable transaction; after any `PARTIAL_WRITE`, run read-only `resume` before retrying.

Runtime inputs are bounded: canonical/payload JSON files are at most 1 MiB, context has at most 256 inputs, a handoff references at most 64 evidence files, and actor/executor identities are single-line values of at most 128 characters. Successful writes report `authorizationValidated: true`.

Do not advertise or enable this writer outside an approved fixture/pilot until the Stage 5 Gate and cross-platform CI pass.
