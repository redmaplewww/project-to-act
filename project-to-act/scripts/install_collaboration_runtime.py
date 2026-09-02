#!/usr/bin/env python3
"""Install the Project-to-Act runtime and tool-neutral adapters without overwriting files."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_transaction import FileTransaction, RuntimeFailure  # noqa: E402


RUNTIME_FILES = (
    "task_view.py",
    "runtime_transaction.py",
    "task_runtime.py",
    "hook_adapter.py",
    "codex_hook_adapter.py",
)
AGENTS_START = "<!-- project-to-act-runtime:start -->"
AGENTS_END = "<!-- project-to-act-runtime:end -->"
DEFAULT_CONFIG = {"schemaVersion": 1, "experimentalHandoffWrites": False}
AGENTS_BLOCK = f"""{AGENTS_START}
## Project-to-Act repository runtime

- Project facts live in `.project-to-act/PROJECT_*.md`; canonical task facts live in `.project-to-act/tasks/<ID>/`.
- At session start run `python .project-to-act/bin/hook_adapter.py --project-root . --event session-start`.
- Before commit run `python .project-to-act/bin/hook_adapter.py --project-root . --event pre-commit`; CI runs the same canonical validator.
- If task discovery is ambiguous, pass `--task-id`; never guess from chat history or agent memory.
- Hooks do not infer role, handoff type, verification verdict, or Gate decisions. Use explicit semantic commands for those changes.
- `COLLABORATION_CONFIG.json` keeps experimental handoff writes disabled unless an approved pilot explicitly enables them.
{AGENTS_END}
"""
PRE_COMMIT = """#!/bin/sh
set -eu
root=$(git rev-parse --show-toplevel)
exec python "$root/.project-to-act/bin/hook_adapter.py" --project-root "$root" --event pre-commit
"""
WORKFLOW = """name: Project-to-Act protocol

on:
  pull_request:

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python .project-to-act/bin/hook_adapter.py --project-root . --event ci
"""
CODEX_HOOK_COMMAND = 'python "$(git rev-parse --show-toplevel)/.project-to-act/bin/codex_hook_adapter.py"'
CODEX_HOOKS = {
    "description": "Project-to-Act repository lifecycle adapter.",
    "hooks": {
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": CODEX_HOOK_COMMAND,
                        "timeout": 10,
                        "statusMessage": "Loading Project-to-Act task context",
                        "additionalContextLimit": 8000,
                    }
                ],
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": CODEX_HOOK_COMMAND,
                        "timeout": 10,
                        "statusMessage": "Checking Project-to-Act task state",
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": CODEX_HOOK_COMMAND,
                        "timeout": 3,
                    }
                ],
            }
        ],
    },
}
CODEX_HOOKS_BYTES = (json.dumps(CODEX_HOOKS, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} must be a regular JSON file", "Repair the existing project configuration.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} is not valid UTF-8 JSON", "Repair the existing project configuration.") from error
    if not isinstance(value, dict):
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} must contain an object", "Repair the existing project configuration.")
    return value


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)


def _managed_root(project_root: Path) -> tuple[Path, Path]:
    root = project_root.expanduser().resolve()
    management = root / ".project-to-act"
    project_config = management / "PROJECT_CONFIG.json"
    if not root.is_dir() or management.is_symlink() or not management.is_dir() or not project_config.is_file():
        raise RuntimeFailure("PROVIDER_NOT_FOUND", "Project-to-Act management is not initialized", "Initialize or adopt Project-to-Act before managing collaboration runtime.", 3)
    config = _read_json(project_config, "PROJECT_CONFIG.json")
    if config.get("schema_version") != 1 or config.get("mode") not in {"managed", "external-ledger"}:
        raise RuntimeFailure("INSTALL_CONFLICT", "Unsupported PROJECT_CONFIG.json", "Migrate the project management configuration first.")
    return root, management


def _static_mappings(root: Path, management: Path) -> list[tuple[Path, bytes]]:
    mappings = []
    for filename in RUNTIME_FILES:
        source = SCRIPT_DIR / filename
        if source.is_symlink() or not source.is_file():
            raise RuntimeFailure("INSTALL_CONFLICT", f"Runtime source is missing: {source}", "Repair the Skill installation.")
        mappings.append((management / "bin" / filename, source.read_bytes()))
    mappings.extend(
        [
            (management / "hooks" / "pre-commit", PRE_COMMIT.encode("utf-8")),
            (root / ".github" / "workflows" / "project-to-act.yml", WORKFLOW.encode("utf-8")),
        ]
    )
    return mappings


def _marker_span(content: str) -> tuple[int, int] | None:
    start_count = content.count(AGENTS_START)
    end_count = content.count(AGENTS_END)
    if start_count != end_count or start_count > 1:
        raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md has an incomplete or duplicate Project-to-Act marker block", "Repair the marker block manually.")
    if start_count == 0:
        return None
    start = content.index(AGENTS_START)
    end = content.index(AGENTS_END, start) + len(AGENTS_END)
    if content[start:end] != AGENTS_BLOCK.rstrip():
        raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md Project-to-Act marker block differs from this runtime", "Review and migrate the marker block explicitly; the installer will not overwrite it.")
    return start, end


def install_runtime(
    project_root: Path,
    *,
    dry_run: bool = False,
    activate_git_hook: bool = False,
    install_agents: bool = True,
    install_codex_hook: bool = False,
) -> dict[str, Any]:
    root, management = _managed_root(project_root)

    mappings: list[tuple[Path, bytes, str]] = []
    mappings.extend((target, content, "created") for target, content in _static_mappings(root, management))

    collaboration_config = management / "COLLABORATION_CONFIG.json"
    config_created = not collaboration_config.exists()
    if config_created:
        mappings.append((collaboration_config, (json.dumps(DEFAULT_CONFIG, indent=2) + "\n").encode("utf-8"), "created"))
    else:
        existing = _read_json(collaboration_config, "COLLABORATION_CONFIG.json")
        if existing.get("schemaVersion") != 1 or not isinstance(existing.get("experimentalHandoffWrites"), bool):
            raise RuntimeFailure("INSTALL_CONFLICT", "Existing collaboration config is incompatible", "Migrate it explicitly; the installer will not overwrite it.")

    agents_path = root / "AGENTS.md"
    agents_action = "skipped"
    agents_content: bytes | None = None
    if install_agents:
        if agents_path.exists():
            if agents_path.is_symlink() or not agents_path.is_file():
                raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md is not a regular file", "Repair AGENTS.md before installing the fallback block.")
            existing_agents = agents_path.read_text(encoding="utf-8")
            marker = _marker_span(existing_agents)
            if marker is not None:
                agents_action = "unchanged"
            else:
                agents_action = "appended"
                agents_content = f"{existing_agents.rstrip()}\n\n{AGENTS_BLOCK}".encode("utf-8")
        else:
            agents_action = "created"
            agents_content = AGENTS_BLOCK.encode("utf-8")
        if agents_content is not None:
            mappings.append((agents_path, agents_content, agents_action))

    codex_hook_action = "skipped"
    codex_hook_path = root / ".codex" / "hooks.json"
    if install_codex_hook:
        codex_hook_action = "created"
        mappings.append((codex_hook_path, CODEX_HOOKS_BYTES, codex_hook_action))

    conflicts = []
    unchanged = []
    writes = []
    for target, content, action in mappings:
        relative = target.relative_to(root).as_posix()
        if target.exists():
            if target.is_symlink() or not target.is_file():
                conflicts.append(relative)
            elif target.read_bytes() == content:
                unchanged.append(relative)
            elif target == agents_path and agents_action == "appended":
                writes.append((target, content))
            else:
                conflicts.append(relative)
        else:
            writes.append((target, content))
    if conflicts:
        raise RuntimeFailure("INSTALL_CONFLICT", f"Installation would overwrite existing files: {conflicts}", "Review or migrate the listed files; the installer made no changes.")

    if not dry_run:
        transaction = FileTransaction(root)
        for target, content in writes:
            transaction.add_text(target, content.decode("utf-8"))
        transaction.commit()
        hook_path = management / "hooks" / "pre-commit"
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    git_hook_active = False
    if activate_git_hook and not dry_run:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeFailure("GIT_FACT_UNAVAILABLE", "Cannot activate hook outside a Git worktree", "Initialize Git or omit --activate-git-hook.")
        configured = _git(root, "config", "core.hooksPath", ".project-to-act/hooks")
        if configured.returncode != 0:
            detail = configured.stderr.strip() or "Unable to configure core.hooksPath"
            raise RuntimeFailure("GIT_FACT_UNAVAILABLE", f"Runtime files were installed, but Git hook activation failed: {detail}", "The installed runtime remains usable; activate the versioned hook manually.")
        git_hook_active = True
    elif not dry_run:
        current = _git(root, "config", "--get", "core.hooksPath")
        git_hook_active = current.returncode == 0 and current.stdout.strip() == ".project-to-act/hooks"

    created_or_updated = [target.relative_to(root).as_posix() for target, _ in writes]
    return {
        "schemaVersion": 1,
        "valid": True,
        "dryRun": dry_run,
        "createdOrUpdated": created_or_updated,
        "unchanged": sorted(set(unchanged)),
        "agentsAction": agents_action,
        "codexHookAction": "unchanged" if codex_hook_path.relative_to(root).as_posix() in unchanged else codex_hook_action,
        "configCreated": config_created,
        "gitHookActive": git_hook_active,
        "gitHookActivation": None if git_hook_active else "git config core.hooksPath .project-to-act/hooks",
    }


def doctor_runtime(project_root: Path, *, check_agents: bool = True, check_codex_hook: bool = False) -> dict[str, Any]:
    root, management = _managed_root(project_root)
    issues = []
    for target, expected in _static_mappings(root, management):
        relative = target.relative_to(root).as_posix()
        if target.is_symlink() or not target.is_file():
            issues.append(f"missing-or-unsafe:{relative}")
        elif target.read_bytes() != expected:
            issues.append(f"content-mismatch:{relative}")
    collaboration_config = management / "COLLABORATION_CONFIG.json"
    try:
        existing = _read_json(collaboration_config, "COLLABORATION_CONFIG.json")
        if existing.get("schemaVersion") != 1 or not isinstance(existing.get("experimentalHandoffWrites"), bool):
            issues.append("incompatible:.project-to-act/COLLABORATION_CONFIG.json")
    except RuntimeFailure:
        issues.append("missing-or-invalid:.project-to-act/COLLABORATION_CONFIG.json")
    agents_status = "skipped"
    if check_agents:
        agents_path = root / "AGENTS.md"
        if agents_path.is_symlink() or not agents_path.is_file():
            issues.append("missing-or-unsafe:AGENTS.md")
        else:
            try:
                marker = _marker_span(agents_path.read_text(encoding="utf-8"))
                if marker is None:
                    issues.append("missing-marker:AGENTS.md")
                else:
                    agents_status = "valid"
            except (UnicodeDecodeError, RuntimeFailure):
                issues.append("invalid-marker:AGENTS.md")
    codex_hook_status = "skipped"
    if check_codex_hook:
        codex_hook_path = root / ".codex" / "hooks.json"
        if codex_hook_path.is_symlink() or not codex_hook_path.is_file():
            issues.append("missing-or-unsafe:.codex/hooks.json")
        elif codex_hook_path.read_bytes() != CODEX_HOOKS_BYTES:
            issues.append("content-mismatch:.codex/hooks.json")
        else:
            codex_hook_status = "valid"
    hook_path = management / "hooks" / "pre-commit"
    if hook_path.is_file() and not os.access(hook_path, os.X_OK):
        issues.append("not-executable:.project-to-act/hooks/pre-commit")
    current = _git(root, "config", "--get", "core.hooksPath")
    git_hook_active = current.returncode == 0 and current.stdout.strip() == ".project-to-act/hooks"
    if issues:
        raise RuntimeFailure("INSTALL_CONFLICT", f"Collaboration runtime doctor found issues: {issues}", "Repair or explicitly reinstall the listed artifacts.")
    return {
        "schemaVersion": 1,
        "valid": True,
        "agentsStatus": agents_status,
        "codexHookStatus": codex_hook_status,
        "gitHookActive": git_hook_active,
    }


def uninstall_runtime(
    project_root: Path,
    *,
    dry_run: bool = False,
    uninstall_agents: bool = True,
) -> dict[str, Any]:
    root, management = _managed_root(project_root)
    deletes: list[Path] = []
    rewrites: list[tuple[Path, str]] = []
    preserved: list[str] = []
    conflicts = []
    for target, expected in _static_mappings(root, management):
        relative = target.relative_to(root).as_posix()
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
            conflicts.append(relative)
        else:
            deletes.append(target)

    collaboration_config = management / "COLLABORATION_CONFIG.json"
    if collaboration_config.exists():
        existing = _read_json(collaboration_config, "COLLABORATION_CONFIG.json")
        if existing == DEFAULT_CONFIG:
            deletes.append(collaboration_config)
        else:
            preserved.append(collaboration_config.relative_to(root).as_posix())

    agents_path = root / "AGENTS.md"
    if uninstall_agents and agents_path.exists():
        if agents_path.is_symlink() or not agents_path.is_file():
            conflicts.append("AGENTS.md")
        else:
            try:
                existing_agents = agents_path.read_text(encoding="utf-8")
                marker = _marker_span(existing_agents)
            except UnicodeDecodeError:
                conflicts.append("AGENTS.md")
            else:
                if marker is None:
                    preserved.append("AGENTS.md")
                else:
                    start, end = marker
                    before = existing_agents[:start]
                    after = existing_agents[end:]
                    if before.endswith("\n\n"):
                        before = before[:-2]
                    if after.startswith("\n"):
                        after = after[1:]
                    remaining = before + after
                    if remaining.strip():
                        rewrites.append((agents_path, remaining))
                    else:
                        deletes.append(agents_path)
    elif agents_path.exists():
        preserved.append("AGENTS.md")

    codex_hook_path = root / ".codex" / "hooks.json"
    if codex_hook_path.exists():
        if codex_hook_path.is_symlink() or not codex_hook_path.is_file():
            conflicts.append(".codex/hooks.json")
        elif codex_hook_path.read_bytes() == CODEX_HOOKS_BYTES:
            deletes.append(codex_hook_path)
        elif "codex_hook_adapter.py" in codex_hook_path.read_text(encoding="utf-8", errors="ignore"):
            conflicts.append(".codex/hooks.json")
        else:
            preserved.append(".codex/hooks.json")

    if conflicts:
        raise RuntimeFailure("INSTALL_CONFLICT", f"Uninstall would remove locally modified artifacts: {sorted(set(conflicts))}", "Restore the installed content or remove the listed artifacts manually; no files were changed.")

    current = _git(root, "config", "--get", "core.hooksPath")
    git_hook_active = current.returncode == 0 and current.stdout.strip() == ".project-to-act/hooks"
    if not dry_run:
        if git_hook_active:
            unset = _git(root, "config", "--unset", "core.hooksPath")
            if unset.returncode != 0:
                raise RuntimeFailure("GIT_FACT_UNAVAILABLE", unset.stderr.strip() or "Unable to unset core.hooksPath", "Deactivate the repository-local hook manually before retrying uninstall.")
        transaction = FileTransaction(root)
        for target, content in rewrites:
            transaction.add_text(target, content)
        for target in deletes:
            transaction.add_delete(target)
        try:
            transaction.commit()
        except RuntimeFailure as error:
            if git_hook_active:
                restored = _git(root, "config", "core.hooksPath", ".project-to-act/hooks")
                if restored.returncode != 0:
                    detail = restored.stderr.strip() or "Unable to restore core.hooksPath"
                    raise RuntimeFailure(
                        "PARTIAL_WRITE",
                        f"File uninstall rolled back, but Git hook activation could not be restored: {detail}",
                        "Restore core.hooksPath=.project-to-act/hooks manually, then run doctor.",
                    ) from error
            raise
        for directory in (management / "bin", management / "hooks", root / ".codex", root / ".github/workflows", root / ".github"):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "schemaVersion": 1,
        "valid": True,
        "dryRun": dry_run,
        "removed": sorted(path.relative_to(root).as_posix() for path in deletes),
        "updated": sorted(path.relative_to(root).as_posix() for path, _ in rewrites),
        "preserved": sorted(set(preserved)),
        "gitHookDeactivated": git_hook_active and not dry_run,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--activate-git-hook", action="store_true")
    parser.add_argument("--skip-agents", action="store_true")
    parser.add_argument("--install-codex-hook", action="store_true")
    parser.add_argument("--doctor-codex-hook", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--doctor", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.doctor:
            if args.dry_run or args.activate_git_hook or args.install_codex_hook:
                raise RuntimeFailure("INSTALL_CONFLICT", "--doctor cannot be combined with --dry-run or --activate-git-hook", "Run doctor as a read-only standalone mode.", 2)
            result = doctor_runtime(args.project_root, check_agents=not args.skip_agents, check_codex_hook=args.doctor_codex_hook)
        elif args.uninstall:
            if args.activate_git_hook or args.install_codex_hook or args.doctor_codex_hook:
                raise RuntimeFailure("INSTALL_CONFLICT", "--uninstall cannot be combined with --activate-git-hook", "Run uninstall without hook activation.", 2)
            result = uninstall_runtime(args.project_root, dry_run=args.dry_run, uninstall_agents=not args.skip_agents)
        else:
            if args.doctor_codex_hook:
                raise RuntimeFailure("INSTALL_CONFLICT", "--doctor-codex-hook requires --doctor", "Run --doctor --doctor-codex-hook together.", 2)
            result = install_runtime(
                args.project_root,
                dry_run=args.dry_run,
                activate_git_hook=args.activate_git_hook,
                install_agents=not args.skip_agents,
                install_codex_hook=args.install_codex_hook,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RuntimeFailure as error:
        print(json.dumps({"code": error.code, "message": str(error), "recovery": error.recovery}, ensure_ascii=False), file=sys.stderr)
        return getattr(error, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
