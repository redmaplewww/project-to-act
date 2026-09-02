"""Shared safety and rollback primitives for the Project-to-Act runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from task_view import ContractError


class RuntimeFailure(ContractError):
    pass


def inside_project(root: Path, path: Path, label: str) -> Path:
    if path.is_symlink():
        raise RuntimeFailure("UNSAFE_PATH", f"{label} must not be a symlink: {path}", "Replace the symlink with a regular repository path.")
    try:
        lexical_relative = path.relative_to(root)
    except ValueError:
        lexical_relative = None
    if lexical_relative is not None:
        current = root
        for part in lexical_relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise RuntimeFailure("UNSAFE_PATH", f"{label} crosses a symlink: {current}", "Replace the symlinked protocol path with a regular directory.")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeFailure("UNSAFE_PATH", f"{label} escapes the project root: {path}", "Use a repository-relative path.") from error
    return resolved


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class FileTransaction:
    """Replace several repository files with rollback on any failed replace."""

    def __init__(self, root: Path, *, fail_after: int | None = None):
        self.root = root.expanduser().resolve()
        self.fail_after = fail_after
        self.changes: list[tuple[Path, bytes | None]] = []

    def add_json(self, path: Path, value: Any) -> None:
        self.changes.append((inside_project(self.root, path, "transaction target"), _json_bytes(value)))

    def add_text(self, path: Path, value: str) -> None:
        self.changes.append((inside_project(self.root, path, "transaction target"), value.encode("utf-8")))

    def add_delete(self, path: Path) -> None:
        self.changes.append((inside_project(self.root, path, "transaction target"), None))

    def commit(self) -> None:
        targets = [path for path, _ in self.changes]
        if len(set(targets)) != len(targets):
            raise RuntimeFailure("PARTIAL_WRITE", "Transaction contains duplicate targets", "Repair the runtime operation before retrying.")
        originals: dict[Path, bytes | None] = {}
        temporary: dict[Path, Path] = {}
        replaced: list[Path] = []
        created_directories: list[Path] = []
        try:
            for target, content in self.changes:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise RuntimeFailure("PARTIAL_WRITE", f"Unsafe transaction target: {target}", "Replace the target with a regular file.")
                missing = []
                current = target.parent
                while current != self.root and not current.exists():
                    missing.append(current)
                    current = current.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_directories.append(directory)
                originals[target] = target.read_bytes() if target.exists() else None
                if content is not None:
                    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                    with temp.open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary[target] = temp

            for target, content in self.changes:
                if content is None:
                    target.unlink()
                else:
                    os.replace(temporary[target], target)
                replaced.append(target)
                if self.fail_after is not None and len(replaced) >= self.fail_after:
                    raise OSError(f"injected failure after {self.fail_after} replacements")
        except BaseException as error:
            rollback_errors = []
            for target in reversed(replaced):
                try:
                    original = originals[target]
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        restore = target.with_name(f".{target.name}.{uuid4().hex}.rollback")
                        restore.write_bytes(original)
                        os.replace(restore, target)
                except BaseException as rollback_error:
                    rollback_errors.append(f"{target}: {rollback_error}")
            for temp in temporary.values():
                temp.unlink(missing_ok=True)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            detail = f"; rollback errors: {rollback_errors}" if rollback_errors else ""
            raise RuntimeFailure("PARTIAL_WRITE", f"Transaction failed and was rolled back: {error}{detail}", "Run resume for a read-only consistency diagnosis before retrying.") from error
