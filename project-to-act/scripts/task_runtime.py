#!/usr/bin/env python3
"""Experimental, repository-local Project-to-Act handoff runtime."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


# Repository-local CLI execution must not create untracked bytecode files.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task_view import MAX_JSON_BYTES, SCHEMA_VERSION, ContractError, canonical_view, role_preview  # noqa: E402
from runtime_transaction import FileTransaction, RuntimeFailure, inside_project as _inside  # noqa: E402


ENABLED_HANDOFF_TYPE = "builder_to_verifier.verification_candidate"
MAX_CONTEXT_INPUTS = 256
MAX_EVIDENCE_REFS = 64
MAX_IDENTITY_LENGTH = 128
MAX_SUMMARY_LENGTH = 4000
MAX_NEXT_ACTION_LENGTH = 2000
MAX_TASKS_SCANNED = 256
MAX_INTENT_ITEMS = 256
MAX_STAGED_PATHS = 4096
ACTIVE_STATES = {"ready", "in_progress", "blocked", "review"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(root: Path, path: Path, label: str) -> dict[str, Any]:
    safe = _inside(root, path, label)
    if safe.is_symlink() or not safe.is_file():
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Missing regular file: {label}", "Restore the required canonical task file.")
    if safe.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"JSON file exceeds {MAX_JSON_BYTES} bytes: {label}", "Reduce the canonical file to the supported contract size.")
    try:
        value = json.loads(safe.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Invalid UTF-8 JSON: {label}", "Repair the canonical task file.") from error
    if not isinstance(value, dict):
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"{label} must contain a JSON object", "Repair the canonical task file.")
    return value


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _git_value(root: Path, *args: str) -> str:
    result = _git(root, *args)
    if result.returncode != 0:
        raise RuntimeFailure("GIT_FACT_UNAVAILABLE", result.stderr.strip() or f"git {' '.join(args)} failed", "Repair the Git repository before retrying.")
    return result.stdout.strip()


def _require_clean(root: Path) -> None:
    status = _git_value(root, "status", "--porcelain=v1", "-uall")
    if status:
        files = [
            line[3:]
            for line in status.splitlines()
            if len(line) > 3 and not line[3:].replace("\\", "/").startswith(".project-to-act/runtime/locks/")
        ]
        if not files:
            return
        raise RuntimeFailure("DIRTY_CHECKPOINT", f"Uncommitted files prevent handoff: {', '.join(files[:8])}", "Commit or remove the changes, then retry.")


def _task_paths(root: Path, task_id: str) -> dict[str, Path]:
    directory = root / ".project-to-act" / "tasks" / task_id
    return {
        "directory": directory,
        "task": directory / "TASK.json",
        "status": directory / "STATUS.json",
        "context": directory / "CONTEXT.json",
        "handoff_json": directory / "HANDOFF.json",
        "handoff_md": directory / "HANDOFF.md",
        "events": directory / "events",
        "evidence": directory / "evidence",
        "sessions": root / ".project-to-act" / "runtime" / "sessions",
        "locks": root / ".project-to-act" / "runtime" / "locks",
    }


def _load_status(root: Path, task_id: str) -> tuple[dict[str, Path], dict[str, Any]]:
    paths = _task_paths(root, task_id)
    status = _read_json(root, paths["status"], f"{task_id}/STATUS.json")
    if status.get("schemaVersion") != SCHEMA_VERSION or status.get("taskId") != task_id:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", "STATUS.json schemaVersion/taskId mismatch", "Repair the task bundle.")
    revision = status.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", "STATUS.revision must be a non-negative integer", "Repair STATUS.json.")
    return paths, status


def _require_enabled(root: Path) -> None:
    config_path = root / ".project-to-act" / "COLLABORATION_CONFIG.json"
    if not config_path.is_file() or config_path.is_symlink():
        raise RuntimeFailure("WRITE_PATH_DISABLED", "Experimental handoff writes are disabled", "Set experimentalHandoffWrites: true in the repository-local collaboration config after review.")
    config = _read_json(root, config_path, "COLLABORATION_CONFIG.json")
    if config.get("schemaVersion") != SCHEMA_VERSION:
        raise RuntimeFailure("WRITE_PATH_DISABLED", "Unsupported collaboration config schema", "Use schemaVersion 1 or disable the candidate writer.")
    if config.get("experimentalHandoffWrites") is not True:
        raise RuntimeFailure("WRITE_PATH_DISABLED", "Experimental handoff writes are disabled", "Set experimentalHandoffWrites: true only in an approved test or pilot repository.")


def _require_revision(status: dict[str, Any], expected_revision: int) -> None:
    if status["revision"] != expected_revision:
        raise RuntimeFailure(
            "STALE_TASK_REVISION",
            f"Expected revision {expected_revision}, found {status['revision']}",
            "Reload the canonical task view and retry from the new revision.",
        )


def _identity(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_IDENTITY_LENGTH or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"{label} must be a single-line identity of at most {MAX_IDENTITY_LENGTH} characters", "Use the canonical actor/executor identity.")
    return normalized


def _current_branch(root: Path) -> str:
    branch = _git_value(root, "branch", "--show-current")
    if branch:
        return branch
    for name in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME", "CI_COMMIT_REF_NAME"):
        candidate = os.environ.get(name, "").strip()
        if candidate:
            return _identity(candidate, name)
    raise RuntimeFailure("BRANCH_MISMATCH", "Git is in detached HEAD and no supported CI branch variable is available", "Pass an explicit task in a branch-aware checkout.")


def _require_branch(root: Path, status: dict[str, Any], expected: str | None = None) -> str:
    branch = expected or status.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise RuntimeFailure("BRANCH_MISMATCH", "STATUS.branch is missing", "Assign one canonical task branch.")
    actual = _current_branch(root)
    if actual != branch:
        raise RuntimeFailure("BRANCH_MISMATCH", f"Current branch {actual!r} does not match task branch {branch!r}", "Switch to the canonical task branch.")
    return branch


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _intent(root: Path, task_id: str) -> dict[str, Any]:
    path = root / ".project-to-act" / "tasks" / task_id / "INTENT.json"
    intent = _read_json(root, path, f"{task_id}/INTENT.json")
    if intent.get("schemaVersion") != SCHEMA_VERSION or intent.get("taskId") != task_id:
        raise RuntimeFailure("INTENT_CONFLICT", f"{task_id}/INTENT.json schemaVersion/taskId mismatch", "Repair the task intent contract.")
    for field in ("paths", "symbols", "contractsWrite"):
        values = intent.get(field)
        if not isinstance(values, list) or len(values) > MAX_INTENT_ITEMS or not all(isinstance(value, str) and value.strip() for value in values):
            raise RuntimeFailure("INTENT_CONFLICT", f"INTENT.{field} must contain at most {MAX_INTENT_ITEMS} non-empty strings", "Repair the task intent contract.")
    normalized_paths = []
    for value in intent["paths"]:
        normalized = value.strip().replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise RuntimeFailure("INTENT_CONFLICT", f"INTENT path must be repository-relative: {value!r}", "Remove absolute or parent-traversal intent paths.")
        normalized_paths.append(normalized.rstrip("/"))
    if not isinstance(intent.get("migrations"), bool):
        raise RuntimeFailure("INTENT_CONFLICT", "INTENT.migrations must be boolean", "Repair the task intent contract.")
    return {
        **intent,
        "paths": normalized_paths,
        "symbols": [value.strip() for value in intent["symbols"]],
        "contractsWrite": [value.strip() for value in intent["contractsWrite"]],
    }


def _path_prefix(pattern: str) -> str:
    normalized = pattern.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    wildcard = re.search(r"[?*\[]", normalized)
    return normalized[: wildcard.start() if wildcard else len(normalized)].rstrip("/")


def _paths_overlap(left: str, right: str) -> bool:
    first = _path_prefix(left)
    second = _path_prefix(right)
    return not first or not second or first == second or first.startswith(f"{second}/") or second.startswith(f"{first}/")


def _intent_conflicts(root: Path, task_id: str) -> list[dict[str, Any]]:
    task_store = root / ".project-to-act" / "tasks"
    directories = sorted(path for path in task_store.iterdir() if path.is_dir() and not path.is_symlink())
    if len(directories) > MAX_TASKS_SCANNED:
        raise RuntimeFailure("INTENT_CONFLICT", f"Task store exceeds {MAX_TASKS_SCANNED} tasks for one runtime check", "Archive or partition inactive task bundles.")
    selected = []
    for directory in directories:
        other_id = directory.name
        _, status = _load_status(root, other_id)
        if status.get("state") in ACTIVE_STATES:
            selected.append({"taskId": other_id, "intent": _intent(root, other_id)})
    current = next((item for item in selected if item["taskId"] == task_id), None)
    if current is None:
        return []
    conflicts = []
    for other in selected:
        if other["taskId"] == task_id:
            continue
        reasons = []
        for left in current["intent"]["paths"]:
            for right in other["intent"]["paths"]:
                if _paths_overlap(left, right):
                    reasons.append({"type": "path", "left": left, "right": right})
        for field in ("symbols", "contractsWrite"):
            overlap = sorted(set(current["intent"][field]) & set(other["intent"][field]))
            reasons.extend({"type": field, "value": value} for value in overlap)
        if current["intent"]["migrations"] and other["intent"]["migrations"]:
            reasons.append({"type": "migration-sequence"})
        if reasons:
            conflicts.append({"tasks": [task_id, other["taskId"]], "reasons": reasons})
    return conflicts


def _require_no_intent_conflicts(root: Path, task_id: str) -> None:
    conflicts = _intent_conflicts(root, task_id)
    if conflicts:
        raise RuntimeFailure("INTENT_CONFLICT", f"Task intent conflicts: {json.dumps(conflicts, ensure_ascii=False)}", "Resolve path/symbol/contract/migration ownership before handoff.")


def discover_task(root: Path, task_id: str | None = None) -> str:
    root = root.expanduser().resolve()
    if task_id:
        _load_status(root, task_id)
        return task_id
    branch = _current_branch(root)
    task_store = root / ".project-to-act" / "tasks"
    if not task_store.is_dir() or task_store.is_symlink():
        raise RuntimeFailure("PROVIDER_NOT_FOUND", "No target task store is available", "Pass --task-id or initialize a canonical target task.", 3)
    matches = []
    directories = sorted(path for path in task_store.iterdir() if path.is_dir() and not path.is_symlink())
    if len(directories) > MAX_TASKS_SCANNED:
        raise RuntimeFailure("CANONICAL_SOURCE_CONFLICT", f"Task store exceeds {MAX_TASKS_SCANNED} discoverable tasks", "Pass an explicit --task-id.", 3)
    for directory in directories:
        _, status = _load_status(root, directory.name)
        if status.get("state") in ACTIVE_STATES and status.get("branch") == branch:
            matches.append(directory.name)
    if not matches:
        raise RuntimeFailure("PROVIDER_NOT_FOUND", f"No active task matches Git branch {branch!r}", "Pass an explicit --task-id or assign STATUS.branch.", 3)
    if len(matches) > 1:
        raise RuntimeFailure("CANONICAL_SOURCE_CONFLICT", f"Multiple active tasks match branch {branch!r}: {matches}", "Pass an explicit --task-id and resolve branch ownership.", 3)
    return matches[0]


def _intent_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if re.search(r"[?*\[]", pattern):
            path_parts = tuple(normalized.split("/"))
            pattern_parts = tuple(pattern.split("/"))

            @lru_cache(maxsize=None)
            def matches(path_index: int, pattern_index: int) -> bool:
                if pattern_index == len(pattern_parts):
                    return path_index == len(path_parts)
                segment = pattern_parts[pattern_index]
                if segment == "**":
                    return matches(path_index, pattern_index + 1) or (
                        path_index < len(path_parts) and matches(path_index + 1, pattern_index)
                    )
                return (
                    path_index < len(path_parts)
                    and fnmatch.fnmatchcase(path_parts[path_index], segment)
                    and matches(path_index + 1, pattern_index + 1)
                )

            if matches(0, 0):
                return True
            continue
        if normalized == pattern or normalized.startswith(f"{pattern}/"):
            return True
    return False


def validate_task(root: Path, task_id: str, *, staged: bool = False) -> dict[str, Any]:
    root = root.expanduser().resolve()
    paths, status = _load_status(root, task_id)
    _require_branch(root, status)
    _check_context(root, task_id, paths, status)
    _require_no_intent_conflicts(root, task_id)
    view = canonical_view(root, task_id)
    staged_paths: list[str] = []
    if staged:
        staged_paths = [line for line in _git_value(root, "diff", "--cached", "--name-only", "--").splitlines() if line]
        if len(staged_paths) > MAX_STAGED_PATHS:
            raise RuntimeFailure("INTENT_CONFLICT", f"Staged path set exceeds {MAX_STAGED_PATHS} files", "Split the commit before validating task intent.")
        intent = _intent(root, task_id)
        protocol_prefixes = (
            f".project-to-act/tasks/{task_id}/",
            ".project-to-act/runtime/",
        )
        outside = [
            path
            for path in staged_paths
            if not path.replace("\\", "/").startswith(protocol_prefixes)
            and not _intent_matches(path, intent["paths"])
        ]
        if outside:
            raise RuntimeFailure("INTENT_CONFLICT", f"Staged paths are outside task intent: {outside}", "Update the approved INTENT.json or remove the staged paths.")
    recovery = resume_task(root, task_id)
    if not recovery["recoverable"]:
        raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", f"Task is not recoverable: {recovery['errors']}", "Repair the canonical handoff/session state before continuing.")
    return {
        "schemaVersion": 1,
        "valid": True,
        "taskId": task_id,
        "staged": staged,
        "stagedPaths": staged_paths,
        "view": view,
        "recovery": recovery,
    }


def _check_context(root: Path, task_id: str, paths: dict[str, Path], status: dict[str, Any]) -> dict[str, Any]:
    context = _read_json(root, paths["context"], f"{task_id}/CONTEXT.json")
    if context.get("schemaVersion") != SCHEMA_VERSION or context.get("taskId") != task_id:
        raise RuntimeFailure("STALE_CONTEXT", "CONTEXT.json schemaVersion/taskId mismatch", "Rebuild the task context.")
    context_hash = context.get("contextHash")
    if not isinstance(context_hash, str) or not context_hash.strip() or status.get("contextHash") != context_hash:
        raise RuntimeFailure("STALE_CONTEXT", "STATUS and CONTEXT contextHash values do not match", "Rebuild context and update STATUS through the canonical runtime.")
    inputs = context.get("inputs")
    if not isinstance(inputs, list):
        raise RuntimeFailure("STALE_CONTEXT", "CONTEXT.inputs must be an array", "Rebuild the task context.")
    if len(inputs) > MAX_CONTEXT_INPUTS:
        raise RuntimeFailure("STALE_CONTEXT", f"CONTEXT.inputs exceeds {MAX_CONTEXT_INPUTS} entries", "Split or reduce the authoritative context.")
    changed = []
    for index, item in enumerate(inputs):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise RuntimeFailure("STALE_CONTEXT", f"CONTEXT.inputs[{index}] is invalid", "Rebuild the task context.")
        source = _inside(root, root / item["path"], f"CONTEXT.inputs[{index}]")
        if source.is_symlink() or not source.is_file():
            changed.append({"path": item["path"], "reason": "missing-or-unsafe"})
        else:
            actual = _file_digest(source)
            if actual != item["sha256"]:
                changed.append({"path": item["path"], "reason": "content-changed"})
    if changed:
        raise RuntimeFailure("STALE_CONTEXT", f"Context inputs changed: {json.dumps(changed, ensure_ascii=False)}", "Rebuild context before handoff.")
    return context


def _require_evidence(root: Path, task_id: str, paths: dict[str, Path], refs: Any) -> list[str]:
    if not isinstance(refs, list) or not refs:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", "verification_candidate requires selfTestEvidenceRefs", "Record at least one passed self-check evidence file.")
    if len(refs) > MAX_EVIDENCE_REFS:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", f"selfTestEvidenceRefs exceeds {MAX_EVIDENCE_REFS} entries", "Reference only the evidence required for this handoff.")
    normalized = []
    evidence_root = paths["evidence"].resolve(strict=False)
    for index, ref in enumerate(refs):
        if not isinstance(ref, str) or not ref.strip():
            raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Invalid evidence ref at index {index}", "Use a repository-relative evidence JSON path.")
        evidence_path = _inside(root, root / ref, f"evidence ref {index}")
        try:
            evidence_path.relative_to(evidence_root)
        except ValueError as error:
            raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Evidence is outside task {task_id}: {ref}", "Reference evidence from the current task directory.") from error
        evidence = _read_json(root, evidence_path, ref)
        if evidence.get("taskId") != task_id:
            raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Evidence taskId mismatch: {ref}", "Reference evidence belonging to the current task.")
        if evidence.get("kind") != "self-check":
            raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Evidence is not a self-check: {ref}", "Reference Builder self-check evidence.")
        outcome = evidence.get("status", evidence.get("result"))
        if outcome != "passed":
            raise RuntimeFailure("HANDOFF_INCOMPLETE", f"Evidence is not passed: {ref}", "Run the required self-check and record a passed result.")
        normalized.append(evidence_path.relative_to(root).as_posix())
    return normalized


@contextmanager
def _task_lock(root: Path, task_id: str, paths: dict[str, Path]) -> Iterator[None]:
    lock_dir = _inside(root, paths["locks"], "runtime lock directory")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{task_id}.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeFailure("ACTIVE_WRITER_CONFLICT", f"Task {task_id} is being updated by another process", "Retry after the active update finishes.") from error
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _render_handoff(handoff: dict[str, Any]) -> str:
    verification = handoff["verification"]
    lines = [
        f"# {handoff['taskId']} Handoff",
        "",
        f"- State: {handoff['state']}",
        f"- Type: `{handoff['handoffType']}`",
        f"- Roles: {handoff['producerRole']} -> {handoff['consumerRole']}",
        f"- From: {handoff['from']}",
        f"- To: {handoff['to']}",
        f"- Branch: `{handoff['branch']}`",
        f"- Code SHA: `{handoff['codeSha']}`",
        f"- Task revision: {handoff['taskRevision']}",
        f"- Context: `{handoff['contextHash']}`",
        f"- Verification: {verification['status']} ({verification['kind']})",
        "",
        "## Current result",
        "",
        handoff["summary"],
        "",
        "## Next action",
        "",
        handoff["nextAction"],
        "",
    ]
    return "\n".join(lines)


def _event(task_id: str, event_type: str, **data: Any) -> tuple[str, dict[str, Any]]:
    timestamp = _now()
    event_id = str(uuid4())
    filename = f"{timestamp.replace(':', '-').replace('.', '-')}-{event_id}.json"
    return filename, {"schemaVersion": 1, "eventId": event_id, "taskId": task_id, "type": event_type, "timestamp": timestamp, **data}


def _same_publish(handoff: dict[str, Any], request: dict[str, Any]) -> bool:
    fields = ("sourceRevision", "from", "to", "branch", "codeSha", "contextHash", "summary", "nextAction", "handoffType", "payload")
    return handoff.get("state") == "published" and all(handoff.get(field) == request.get(field) for field in fields)


def publish_handoff(
    root: Path,
    task_id: str,
    *,
    expected_revision: int,
    from_actor: str,
    to_actor: str,
    handoff_type: str,
    payload: dict[str, Any],
    summary: str,
    next_action: str,
    fail_after: int | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _require_enabled(root)
    if handoff_type != ENABLED_HANDOFF_TYPE:
        raise RuntimeFailure("WRITE_PATH_DISABLED", f"Stage 5 only writes {ENABLED_HANDOFF_TYPE}", "Use role-preview for other handoff types.")
    from_actor = _identity(from_actor, "from actor")
    to_actor = _identity(to_actor, "to actor")
    summary = summary.strip()
    next_action = next_action.strip()
    if not summary or not next_action:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", "from/to/summary/nextAction must be non-empty", "Complete the handoff request.")
    if len(summary) > MAX_SUMMARY_LENGTH or len(next_action) > MAX_NEXT_ACTION_LENGTH:
        raise RuntimeFailure("HANDOFF_INCOMPLETE", "summary or nextAction exceeds the supported length", "Keep the handoff concise and move detail into referenced evidence.")

    paths, _ = _load_status(root, task_id)
    with _task_lock(root, task_id, paths):
        paths, status = _load_status(root, task_id)
        branch = _require_branch(root, status)
        code_sha = _git_value(root, "rev-parse", "HEAD")
        context = _check_context(root, task_id, paths, status)
        _require_no_intent_conflicts(root, task_id)
        view = canonical_view(root, task_id)
        preview = role_preview(view, handoff_type, payload)
        evidence_refs = _require_evidence(root, task_id, paths, payload.get("selfTestEvidenceRefs"))
        request = {
            "sourceRevision": expected_revision,
            "from": from_actor,
            "to": to_actor,
            "branch": branch,
            "codeSha": code_sha,
            "contextHash": context["contextHash"],
            "summary": summary,
            "nextAction": next_action,
            "handoffType": handoff_type,
            "payload": payload,
        }
        if paths["handoff_json"].is_file() and not paths["handoff_json"].is_symlink():
            existing = _read_json(root, paths["handoff_json"], f"{task_id}/HANDOFF.json")
            if (
                _same_publish(existing, request)
                and status.get("handoffId") == existing.get("handoffId")
                and status.get("revision") == existing.get("taskRevision")
                and status.get("handoffState") == "published"
                and status.get("currentActor") is None
                and status.get("currentRole") is None
                and status.get("activeSessionId") is None
            ):
                return {"schemaVersion": 1, "noOp": True, "code": "NO_OP", "handoff": existing, "status": status}

        _require_revision(status, expected_revision)
        if status.get("currentActor") != from_actor:
            raise RuntimeFailure("ACTIVE_WRITER_CONFLICT", f"Current actor {status.get('currentActor')!r} is not publisher {from_actor!r}", "Publish from the active task writer.")
        if status.get("currentRole") != preview["producerRole"]:
            raise RuntimeFailure(
                "ROLE_DIRECTION_CONFLICT",
                f"Current role {status.get('currentRole')!r} is not producer role {preview['producerRole']!r}",
                "Assign the task-scoped producer role through the canonical task contract before publishing.",
            )
        _require_clean(root)
        published_at = _now()
        next_revision = expected_revision + 1
        handoff_id = f"h-{uuid4()}"
        handoff = {
            "schemaVersion": 1,
            "handoffId": handoff_id,
            "taskId": task_id,
            "state": "published",
            "sourceRevision": expected_revision,
            "taskRevision": next_revision,
            "from": from_actor,
            "to": to_actor,
            "branch": branch,
            "codeSha": code_sha,
            "contextHash": context["contextHash"],
            "summary": summary,
            "completed": [],
            "pending": [],
            "decisions": [],
            "nextAction": next_action,
            "verification": {"status": "passed", "kind": "self-check", "evidenceRefs": evidence_refs},
            "producerRole": preview["producerRole"],
            "consumerRole": preview["consumerRole"],
            "handoffType": handoff_type,
            "payloadSchema": preview["payloadSchema"],
            "payload": payload,
            "publishedAt": published_at,
            "acceptedBy": None,
            "acceptedAt": None,
            "acceptedRevision": None,
        }
        next_status = {
            **status,
            "revision": next_revision,
            "currentActor": None,
            "currentRole": None,
            "activeSessionId": None,
            "handoffState": "published",
            "handoffId": handoff_id,
            "headSha": code_sha,
            "nextAction": next_action,
            "updatedAt": published_at,
        }
        event_name, event = _event(
            task_id,
            "handoff-published",
            handoffId=handoff_id,
            handoffType=handoff_type,
            fromActor=from_actor,
            toActor=to_actor,
            codeSha=code_sha,
            contextHash=context["contextHash"],
            revision=next_revision,
        )
        transaction = FileTransaction(root, fail_after=fail_after)
        transaction.add_json(paths["handoff_json"], handoff)
        transaction.add_text(paths["handoff_md"], _render_handoff(handoff))
        transaction.add_json(paths["status"], next_status)
        transaction.add_json(paths["events"] / event_name, event)
        transaction.commit()
        return {"schemaVersion": 1, "noOp": False, "authorizationValidated": True, "handoff": handoff, "status": next_status, "event": event}


def accept_handoff(
    root: Path,
    task_id: str,
    *,
    expected_revision: int,
    actor: str,
    executor: str,
    fail_after: int | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _require_enabled(root)
    actor = _identity(actor, "actor")
    executor = _identity(executor, "executor")
    paths, _ = _load_status(root, task_id)
    with _task_lock(root, task_id, paths):
        paths, status = _load_status(root, task_id)
        handoff = _read_json(root, paths["handoff_json"], f"{task_id}/HANDOFF.json")
        if handoff.get("state") == "accepted" and handoff.get("acceptedBy") == actor:
            if handoff.get("taskRevision") != expected_revision:
                _require_revision(status, expected_revision)
            session_id = status.get("activeSessionId")
            session = _read_json(root, paths["sessions"] / f"{session_id}.json", f"session {session_id}") if session_id else None
            if (
                status.get("handoffState") == "accepted"
                and status.get("currentActor") == actor
                and status.get("currentRole") == handoff.get("consumerRole")
                and session is not None
                and session.get("taskId") == task_id
                and session.get("actorId") == actor
                and session.get("role") == handoff.get("consumerRole")
                and session.get("executor") == executor
                and session.get("handoffId") == handoff.get("handoffId")
                and session.get("startedRevision") == status.get("revision")
            ):
                return {"schemaVersion": 1, "noOp": True, "code": "NO_OP", "handoff": handoff, "status": status, "session": session}
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "Accepted handoff/session state is inconsistent with the retry", "Run resume and repair the canonical state before retrying.")

        _require_revision(status, expected_revision)
        branch = _require_branch(root, status, handoff.get("branch"))
        _require_clean(root)
        if handoff.get("schemaVersion") != 1 or handoff.get("taskId") != task_id or handoff.get("state") != "published":
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "HANDOFF.json is not a published snapshot for this task", "Reload the canonical handoff snapshot.")
        if handoff.get("handoffType") != ENABLED_HANDOFF_TYPE:
            raise RuntimeFailure("WRITE_PATH_DISABLED", "This handoff type is not enabled for accept", "Use the Stage 5 verification-candidate path.")
        if handoff.get("to") not in {"any", actor}:
            raise RuntimeFailure("ACTIVE_WRITER_CONFLICT", f"Handoff target is {handoff.get('to')!r}, not {actor!r}", "Use the named receiving actor.")
        if handoff.get("taskRevision") != expected_revision or status.get("handoffId") != handoff.get("handoffId"):
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "Handoff id or task revision does not match STATUS", "Reload the task and handoff snapshot.")
        if status.get("contextHash") != handoff.get("contextHash"):
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "Handoff contextHash does not match STATUS", "Rebuild and republish the handoff.")
        if status.get("currentActor") or status.get("activeSessionId"):
            raise RuntimeFailure("ACTIVE_WRITER_CONFLICT", "Task already has an active writer or session", "Stop the active writer before accepting.")
        context = _check_context(root, task_id, paths, status)
        _require_no_intent_conflicts(root, task_id)
        if context.get("contextHash") != handoff.get("contextHash"):
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "Handoff context anchor is stale", "Rebuild and republish the handoff.")
        ancestor = _git(root, "merge-base", "--is-ancestor", str(handoff.get("codeSha")), "HEAD")
        if ancestor.returncode != 0:
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", f"Code anchor {handoff.get('codeSha')!r} is not in current history", "Pull the branch containing the published code SHA.")
        view = canonical_view(root, task_id)
        preview = role_preview(view, handoff["handoffType"], handoff.get("payload"))
        if handoff.get("producerRole") != preview["producerRole"] or handoff.get("consumerRole") != preview["consumerRole"]:
            raise RuntimeFailure("ROLE_DIRECTION_CONFLICT", "Stored role direction does not match the handoff contract", "Republish a valid canonical handoff.")
        evidence_refs = _require_evidence(root, task_id, paths, handoff.get("payload", {}).get("selfTestEvidenceRefs"))
        if handoff.get("verification") != {"status": "passed", "kind": "self-check", "evidenceRefs": evidence_refs}:
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", "Stored verification evidence does not match the role payload", "Republish the handoff from fresh evidence.")

        accepted_at = _now()
        next_revision = expected_revision + 1
        session_id = f"s-{uuid4()}"
        session = {
            "schemaVersion": 1,
            "sessionId": session_id,
            "taskId": task_id,
            "actorId": actor,
            "role": preview["consumerRole"],
            "executor": executor,
            "state": "active",
            "handoffId": handoff["handoffId"],
            "baseRevision": expected_revision,
            "startedRevision": next_revision,
            "startedAt": accepted_at,
        }
        next_handoff = {
            **handoff,
            "state": "accepted",
            "acceptedBy": actor,
            "acceptedAt": accepted_at,
            "acceptedRevision": next_revision,
        }
        next_status = {
            **status,
            "revision": next_revision,
            "currentActor": actor,
            "currentRole": preview["consumerRole"],
            "activeSessionId": session_id,
            "handoffState": "accepted",
            "updatedAt": accepted_at,
        }
        event_name, event = _event(
            task_id,
            "handoff-accepted",
            handoffId=handoff["handoffId"],
            actorId=actor,
            executor=executor,
            codeSha=handoff["codeSha"],
            contextHash=handoff["contextHash"],
            revision=next_revision,
            sessionId=session_id,
        )
        transaction = FileTransaction(root, fail_after=fail_after)
        transaction.add_json(paths["handoff_json"], next_handoff)
        transaction.add_text(paths["handoff_md"], _render_handoff(next_handoff))
        transaction.add_json(paths["status"], next_status)
        transaction.add_json(paths["sessions"] / f"{session_id}.json", session)
        transaction.add_json(paths["events"] / event_name, event)
        transaction.commit()
        return {"schemaVersion": 1, "noOp": False, "authorizationValidated": True, "handoff": next_handoff, "status": next_status, "session": session, "event": event, "branch": branch}


def resume_task(root: Path, task_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    paths, status = _load_status(root, task_id)
    _require_branch(root, status)
    view = canonical_view(root, task_id)
    errors = []
    handoff = None
    session = None
    if status.get("handoffState") in {"published", "accepted"}:
        try:
            handoff = _read_json(root, paths["handoff_json"], f"{task_id}/HANDOFF.json")
        except RuntimeFailure as error:
            errors.append({"code": error.code, "message": str(error)})
        if handoff:
            if handoff.get("schemaVersion") != SCHEMA_VERSION or handoff.get("taskId") != task_id:
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "HANDOFF schemaVersion/taskId mismatch"})
            if handoff.get("handoffId") != status.get("handoffId"):
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "STATUS.handoffId does not match HANDOFF.json"})
            expected_revision = handoff.get("acceptedRevision") if handoff.get("state") == "accepted" else handoff.get("taskRevision")
            if status.get("revision") != expected_revision:
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "STATUS.revision does not match the handoff lifecycle revision"})
            if status.get("contextHash") != handoff.get("contextHash"):
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "STATUS.contextHash does not match HANDOFF.json"})
            if handoff.get("state") != status.get("handoffState"):
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "STATUS.handoffState does not match HANDOFF.state"})
            actual_branch = _current_branch(root)
            if handoff.get("branch") != status.get("branch") or handoff.get("branch") != actual_branch:
                errors.append({"code": "BRANCH_MISMATCH", "message": "Current branch, STATUS.branch, and HANDOFF.branch do not match"})
            code_sha = handoff.get("codeSha")
            if not isinstance(code_sha, str) or _git(root, "merge-base", "--is-ancestor", code_sha, "HEAD").returncode != 0:
                errors.append({"code": "HANDOFF_ANCHOR_MISMATCH", "message": "HANDOFF.codeSha is not in current Git history"})
            try:
                role_preview(view, handoff.get("handoffType"), handoff.get("payload"))
            except ContractError as error:
                errors.append({"code": error.code, "message": str(error)})
            if handoff.get("state") == "published" and (status.get("currentActor") or status.get("activeSessionId")):
                errors.append({"code": "ACTIVE_WRITER_CONFLICT", "message": "Published handoff must not retain an active writer/session"})
            if handoff.get("state") == "published" and status.get("currentRole"):
                errors.append({"code": "ROLE_DIRECTION_CONFLICT", "message": "Published handoff must not retain a current role"})
            if handoff.get("state") == "accepted" and status.get("currentRole") != handoff.get("consumerRole"):
                errors.append({"code": "ROLE_DIRECTION_CONFLICT", "message": "Accepted task role does not match HANDOFF.consumerRole"})
    if status.get("activeSessionId"):
        try:
            session = _read_json(root, paths["sessions"] / f"{status['activeSessionId']}.json", f"session {status['activeSessionId']}")
            if (
                session.get("taskId") != task_id
                or session.get("actorId") != status.get("currentActor")
                or session.get("role") != status.get("currentRole")
                or session.get("state") != "active"
                or session.get("handoffId") != status.get("handoffId")
                or session.get("startedRevision") != status.get("revision")
            ):
                errors.append({"code": "ACTIVE_WRITER_CONFLICT", "message": "Active session does not match STATUS"})
        except RuntimeFailure as error:
            errors.append({"code": error.code, "message": str(error)})
    elif status.get("currentActor") and status.get("handoffState") == "accepted":
        errors.append({"code": "ACTIVE_WRITER_CONFLICT", "message": "Accepted task has a current actor but no active session"})
    try:
        _check_context(root, task_id, paths, status)
    except RuntimeFailure as error:
        errors.append({"code": error.code, "message": str(error)})
    return {
        "schemaVersion": 1,
        "recoverable": not errors,
        "view": view,
        "handoff": handoff,
        "session": session,
        "nextAction": handoff.get("nextAction") if handoff else view.get("nextAction"),
        "errors": errors,
    }


def _payload(path: str) -> dict[str, Any]:
    payload_path = Path(path).expanduser().resolve()
    if payload_path.is_symlink() or not payload_path.is_file():
        raise RuntimeFailure("ROLE_PAYLOAD_INVALID", f"Payload file is missing or unsafe: {payload_path}", "Provide a regular JSON payload file.")
    if payload_path.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeFailure("ROLE_PAYLOAD_INVALID", f"Payload exceeds {MAX_JSON_BYTES} bytes", "Reduce the payload and reference evidence instead of embedding it.")
    try:
        value = json.loads(payload_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("ROLE_PAYLOAD_INVALID", "Payload file is not valid UTF-8 JSON", "Repair the payload file.") from error
    if not isinstance(value, dict):
        raise RuntimeFailure("ROLE_PAYLOAD_INVALID", "Payload must be a JSON object", "Repair the payload file.")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    handoff = commands.add_parser("handoff")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    publish = handoff_commands.add_parser("publish")
    publish.add_argument("task_id")
    publish.add_argument("--expected-revision", type=int, required=True)
    publish.add_argument("--from", dest="from_actor", required=True)
    publish.add_argument("--to", dest="to_actor", required=True)
    publish.add_argument("--handoff-type", required=True)
    publish.add_argument("--payload-file", required=True)
    publish.add_argument("--summary", required=True)
    publish.add_argument("--next-action", required=True)
    accept = handoff_commands.add_parser("accept")
    accept.add_argument("task_id")
    accept.add_argument("--expected-revision", type=int, required=True)
    accept.add_argument("--actor", required=True)
    accept.add_argument("--executor", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("task_id", nargs="?")
    validate = commands.add_parser("validate")
    validate.add_argument("task_id", nargs="?")
    validate.add_argument("--staged", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = Path(args.project_root)
        if args.command == "handoff" and args.handoff_command == "publish":
            result = publish_handoff(
                root,
                args.task_id,
                expected_revision=args.expected_revision,
                from_actor=args.from_actor,
                to_actor=args.to_actor,
                handoff_type=args.handoff_type,
                payload=_payload(args.payload_file),
                summary=args.summary,
                next_action=args.next_action,
            )
        elif args.command == "handoff" and args.handoff_command == "accept":
            result = accept_handoff(root, args.task_id, expected_revision=args.expected_revision, actor=args.actor, executor=args.executor)
        elif args.command == "resume":
            selected_task = discover_task(root, args.task_id)
            result = resume_task(root, selected_task)
            if not result["recoverable"]:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        else:
            selected_task = discover_task(root, args.task_id)
            result = validate_task(root, selected_task, staged=args.staged)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ContractError as error:
        print(json.dumps({"code": error.code, "message": str(error), "recovery": error.recovery}, ensure_ascii=False), file=sys.stderr)
        return getattr(error, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
