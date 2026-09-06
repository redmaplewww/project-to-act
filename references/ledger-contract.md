# Project-to-Act v2 ledger contract

Read this reference when auditing, migrating, compacting, restoring, or deciding whether content belongs in a managed document.

## Active document ownership

| Document | Active content | Content that must be linked instead of embedded |
|---|---|---|
| `PROJECT_OVERVIEW.md` | objective, scope, non-goals, constraints, current focus, latest route decisions | experiment narratives, per-run status, detailed architecture, Skill instructions |
| `PROJECT_PROGRESS.md` | current tasks, blockers, next actions, latest progress summaries | completed-task detail, terminal output, complete work logs |
| `PROJECT_FEATURES.md` | feature registry, priority, state, dependencies, completion conditions | APIs, state machines, schemas, module/function design |
| `PROJECT_VERSIONS.md` | current and next version, compatibility, latest release summaries | work-package reports, patch narratives, test output |
| `PROJECT_ACCEPTANCE.md` | current conclusion, active criteria and Gates, referenced or fresh evidence | raw output, long commands, complete reports, evidence-by-evidence essays |

Normal project documents under `docs/`, `artifacts/`, test-result folders, or another project-selected location remain authoritative for their own detailed content. Project-to-Act is the canonical governance index and current status, not the canonical copy of every project fact.

Only the five named `PROJECT_*.md` files belong at the management root. Additional knowledge, design, runbook, or report documents belong outside `.project-to-act` and are referenced by path and hash when governance needs them.

## Bounded active history

Schema v2 defaults retain the latest ten records in each history section. Acceptance additionally retains every evidence row referenced by the current conclusion, criteria, or Gate sections plus the latest twenty unreferenced evidence rows.

Older records are appended verbatim to:

```text
.project-to-act/archive/<domain>/YYYY-MM.md
```

The domains are `overview`, `progress`, `features`, `versions`, and `acceptance`. Records without a parseable date use `legacy-undated.md`. Archive files are append-only. JSON manifests contain integrity and compressed recovery data, not governance history.

## Audit severity

Errors always fail validation:

- missing or duplicate required headings;
- invalid configuration or unsupported schema;
- unsafe links, reparse points, or paths outside the project root;
- duplicate canonical IDs in the same registry;
- broken archive references.

Warnings do not fail ordinary validation, but fail with `--strict`:

- active document over its configured byte budget;
- unexpected second-level headings or extra root `PROJECT_*.md` files;
- code fences, raw-output patterns, very long table cells, or excessive subsections;
- evidence references absent from the evidence index;
- schema v1 that has not been migrated.

Audit warnings identify candidates. They do not authorize automatic deletion or relocation.

## Migration, compaction, and recovery

- `--migrate --dry-run` previews configuration-only migration. Migration upgrades legacy or schema v1 configuration and fills missing templates without relocating existing content.
- `--compact --dry-run` reports exact records, archive partitions, retained counts, and manual-review sections without writing.
- `--compact` writes archives first, verifies source hashes again, atomically rewrites active documents, then writes a recovery manifest.
- Unknown or mixed-format custom sections are never moved automatically.
- `--restore-compaction <manifest>` restores the pre-compaction active documents after verifying their current post-compaction hashes. Append-only archives remain in place so evidence is never silently erased.

External-ledger mode supports discovery, validation, and audit only. Project-to-Act does not own the external ledger layout and therefore never compacts it.

## Lightweight feature reconciliation

Feature reconciliation is opt-in. Configure exact project-relative files in `feature_reconcile.expected_sources` and `feature_reconcile.implemented_sources`; directories, globs, and repository-wide discovery are rejected. Prefer a short feature manifest over listing large source trees.

`--reconcile-features` compares explicit `F-*` identifiers from those files with `PROJECT_FEATURES.md`. It reports only identifier differences and invalid completion claims. It does not copy source text into the ledger or model context.

Before reading source content, the command fingerprints the configured paths, sizes, modification times, configuration, and feature registry. A successful result is stored under `.project-to-act/state/feature_reconcile.json`. When the fingerprint is unchanged, the next call returns `status=skipped`, `reason=inputs_unchanged`, and `source_files_read=0`. An exclusive lock prevents concurrent duplicate reconciliation.

Ordinary `--check`, `--validate`, `--audit`, initialization, maintenance, and completion work never call feature reconciliation. Run it only when the user requests a full feature comparison, configured feature sources change, or a release Gate explicitly requires it.

Progress history may include a compact conversation outcome: session or task ID, confirmed goal or correction, completed work, evidence, unresolved issue, and next action. Never store a transcript, hidden reasoning, secrets, personal information, or unrelated conversation.
