#!/usr/bin/env python3
"""Translate official Codex lifecycle hook input to Project-to-Act events."""

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

from hook_adapter import handle_event  # noqa: E402
from task_view import ContractError  # noqa: E402


MAX_CODEX_HOOK_INPUT_BYTES = 128 * 1024
MAX_CODEX_HOOK_OUTPUT_CHARS = 8000
EVENT_MAP = {
    "SessionStart": "session-start",
    "Stop": "stop",
    "SessionEnd": "session-end",
}


def _clip(value: str) -> str:
    if len(value) <= MAX_CODEX_HOOK_OUTPUT_CHARS:
        return value
    return value[: MAX_CODEX_HOOK_OUTPUT_CHARS - 14] + "...[truncated]"


def _repository_root(cwd: str) -> Path:
    working = Path(cwd).expanduser().resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=working,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "Codex hook cwd is not inside a Git repository")
    return Path(result.stdout.strip()).resolve()


def adapt_codex_event(payload: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any] | None:
    event_name = payload.get("hook_event_name")
    cwd = payload.get("cwd")
    if event_name not in EVENT_MAP or not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("Codex hook input requires a supported hook_event_name and non-empty cwd")
    root = _repository_root(cwd)
    result = handle_event(root, EVENT_MAP[event_name], task_id=task_id)
    if event_name == "SessionStart":
        context = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _clip(f"Project-to-Act canonical resume result:\n{context}"),
            },
        }
    if event_name == "Stop":
        if result.get("code") == "SEMANTIC_ACTION_REQUIRED":
            paths = result.get("changedPaths", [])
            suffix = " (truncated)" if result.get("changedPathsTruncated") else ""
            return {
                "continue": True,
                "systemMessage": _clip(f"Project-to-Act: {result.get('message')} Changed paths{suffix}: {paths}"),
            }
        return {"continue": True}
    return None


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_CODEX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_CODEX_HOOK_INPUT_BYTES:
        raise ValueError(f"Codex hook input exceeds {MAX_CODEX_HOOK_INPUT_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Codex hook input must be a UTF-8 JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("Codex hook input must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output = adapt_codex_event(_read_payload(), task_id=args.task_id)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ValueError, ContractError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
