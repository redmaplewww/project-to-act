#!/usr/bin/env python3
"""Normalize agent/Git/CI lifecycle events onto the Project-to-Act runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


# Repository-local lifecycle execution must not create untracked bytecode files.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task_runtime import (  # noqa: E402
    ContractError,
    RuntimeFailure,
    canonical_view,
    discover_task,
    resume_task,
    validate_task,
)


EVENTS = {"session-start", "stop", "session-end", "pre-commit", "ci"}
MAX_DISCLOSED_PATHS = 256


def _working_tree(root: Path) -> tuple[list[str], int]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-uall"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure("GIT_FACT_UNAVAILABLE", result.stderr.strip() or "Unable to read Git status", "Repair the Git repository.")
    paths = [line[3:] for line in result.stdout.splitlines() if len(line) > 3 and not line[3:].replace("\\", "/").startswith(".project-to-act/runtime/locks/")]
    return paths[:MAX_DISCLOSED_PATHS], len(paths)


def handle_event(root: Path, event: str, *, task_id: str | None = None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if event not in EVENTS:
        raise RuntimeFailure("CONTRACT_VERSION_UNSUPPORTED", f"Unknown normalized event: {event}", "Use session-start, stop, session-end, pre-commit, or ci.", 2)
    selected = discover_task(root, task_id)
    if event == "session-start":
        recovery = resume_task(root, selected)
        if not recovery["recoverable"]:
            raise RuntimeFailure("HANDOFF_ANCHOR_MISMATCH", f"Task cannot be resumed: {recovery['errors']}", "Repair the canonical state before starting the session.")
        return {"schemaVersion": 1, "event": event, "taskId": selected, "action": "resume", "result": recovery}
    if event == "pre-commit":
        result = validate_task(root, selected, staged=True)
        return {"schemaVersion": 1, "event": event, "taskId": selected, "action": "validate-staged", "result": result}
    if event == "ci":
        result = validate_task(root, selected, staged=False)
        return {"schemaVersion": 1, "event": event, "taskId": selected, "action": "validate", "result": result}

    changed, changed_count = _working_tree(root)
    view = canonical_view(root, selected)
    code = "SEMANTIC_ACTION_REQUIRED" if changed else "NO_OP"
    return {
        "schemaVersion": 1,
        "event": event,
        "taskId": selected,
        "action": "disclose",
        "noOp": True,
        "code": code,
        "changedPaths": changed,
        "changedPathCount": changed_count,
        "changedPathsTruncated": changed_count > len(changed),
        "view": view,
        "message": "Uncommitted work exists; choose an explicit checkpoint, blocker, or handoff action if semantics changed." if changed else "No repository change requires reconciliation.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--event", required=True, choices=sorted(EVENTS))
    parser.add_argument("--task-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = handle_event(Path(args.project_root), args.event, task_id=args.task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ContractError as error:
        print(json.dumps({"code": error.code, "message": str(error), "recovery": error.recovery}, ensure_ascii=False), file=sys.stderr)
        return getattr(error, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
