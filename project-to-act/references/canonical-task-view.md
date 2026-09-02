# Canonical task view v1

Read this reference when a repository contains `.project-to-act/tasks/<ID>/`, when comparing a legacy task with a target task, or when preparing a migration preview.

Run the read-only provider:

```text
python <Skill directory>/scripts/task_view.py view --project-root <root> --task-id <ID>
```

The output is `canonical-task-view@1`. It preserves `provider` and `source`, uses canonical task state separately from `handoffState`, and reports unavailable source semantics in `gaps`. A non-empty `gaps` array is a successful but lossy view, not permission to infer missing facts.

Core fields are:

```text
schemaVersion, provider, source, taskId, title, goal, scope,
owner, nextOwner, state, handoffState, revision, baseRevision,
acceptance, verification, evidenceRefs, nextAction, conflicts, gaps
```

Do not compare providers by deleting `gaps`. Parity means observable task semantics agree and every unavoidable difference is explicitly attributed to the source format.

If both `.ai-team/TASK.md` and a target task bundle exist, require an explicit canonical provider. Do not select by timestamp and do not write both.
