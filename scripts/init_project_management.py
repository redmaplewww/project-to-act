#!/usr/bin/env python3
"""Discover, initialize, audit, migrate, compact, and restore Project-to-Act ledgers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
CONFIG_NAME = "PROJECT_CONFIG.json"
TEMPLATE_NAMES = (
    "PROJECT_OVERVIEW.md",
    "PROJECT_PROGRESS.md",
    "PROJECT_VERSIONS.md",
    "PROJECT_FEATURES.md",
    "PROJECT_ACCEPTANCE.md",
)
EXISTING_LEDGER_CANDIDATES = (
    "PROJECT_LEDGER.md",
    "AGENT_PROJECT.md",
    ".agent/project.md",
    "docs/project-ledger.md",
)
REDIRECT_PREFIX = "<!-- project-to-act-redirect:"
ARCHIVE_MARKER_PREFIX = "<!-- pta-compaction:"
FEATURE_RECONCILE_VERSION = 1
FEATURE_RECONCILE_STATE = "state/feature_reconcile.json"
FEATURE_RECONCILE_LOCK = "state/feature_reconcile.lock"

DEFAULT_POLICY: dict[str, Any] = {
    "history_keep": 10,
    "evidence_keep_unreferenced": 20,
    "archive_partition": "monthly",
    "size_warning_bytes": {
        "PROJECT_OVERVIEW.md": 24 * 1024,
        "PROJECT_PROGRESS.md": 32 * 1024,
        "PROJECT_FEATURES.md": 64 * 1024,
        "PROJECT_VERSIONS.md": 24 * 1024,
        "PROJECT_ACCEPTANCE.md": 64 * 1024,
    },
}

DEFAULT_FEATURE_RECONCILE: dict[str, Any] = {
    "expected_sources": [],
    "implemented_sources": [],
    "max_files": 32,
    "max_total_bytes": 2 * 1024 * 1024,
}

DOC_SPECS: dict[str, dict[str, Any]] = {
    "PROJECT_OVERVIEW.md": {
        "domain": "overview",
        "required": ("## 项目目标", "## 范围", "## 当前焦点"),
        "allowed": (
            "## 基本信息",
            "## 项目目标",
            "## 范围",
            "## 技术路线与关键约束",
            "## 数据与安全边界",
            "## 当前焦点",
            "## 路线变更记录",
        ),
        "history": ("路线变更记录",),
        "registries": (("路线变更记录", "D-"),),
    },
    "PROJECT_PROGRESS.md": {
        "domain": "progress",
        "required": ("## 当前任务", "## 阻塞项", "## 进度历史"),
        "allowed": ("## 当前任务", "## 阻塞项", "## 下一步", "## 进度历史"),
        "history": ("进度历史",),
        "registries": (),
    },
    "PROJECT_VERSIONS.md": {
        "domain": "versions",
        "required": ("## 当前版本", "## 版本历史"),
        "allowed": (
            "## 当前版本",
            "## 下一版本计划",
            "## 兼容性与迁移政策",
            "## 版本历史",
        ),
        "history": ("版本历史",),
        "registries": (),
    },
    "PROJECT_FEATURES.md": {
        "domain": "features",
        "required": ("## 状态定义", "## 功能清单", "## 功能变更历史"),
        "allowed": ("## 状态定义", "## 功能清单", "## 功能变更历史"),
        "history": ("功能变更历史",),
        "registries": (("功能清单", "F-"),),
    },
    "PROJECT_ACCEPTANCE.md": {
        "domain": "acceptance",
        "required": ("## 当前验收结论", "## 验收标准", "## 验收记录"),
        "allowed": (
            "## 当前验收结论",
            "## 验收标准",
            "## 证据索引",
            "## Gate 记录",
            "## 验收记录",
        ),
        "history": ("验收记录",),
        "registries": (
            ("验收标准", "A-"),
            ("证据索引", "E-"),
            ("Gate 记录", "G-"),
        ),
    },
}

ID_RE = re.compile(r"\b([A-Z]+-[A-Za-z0-9][A-Za-z0-9._-]*)\b")
DATE_MONTH_RE = re.compile(r"\b(20\d{2}-(?:0[1-9]|1[0-2]))(?:-[0-3]\d)?\b")
MARKDOWN_LINK_RE = re.compile(r"\]\((archive/[^)]+)\)")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
RAW_OUTPUT_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|^\s*(?:stdout|stderr)\s*:|"
    r"^\d{4}-\d{2}-\d{2}[^\n]*(?:DEBUG|INFO|ERROR|CRITICAL))",
    re.IGNORECASE | re.MULTILINE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_link_or_reparse_point(path: Path, label: str) -> None:
    if path.is_symlink():
        raise OSError(f"{label}不得是符号链接：{path}")
    if _is_windows_reparse_point(path):
        raise OSError(f"{label}不得是 Windows 重解析点：{path}")


def _resolve_project_root(project_root: Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"项目根路径不存在：{root}")
    if not root.is_dir():
        raise ValueError(f"项目根路径不是目录：{root}")
    _reject_link_or_reparse_point(root, "项目根路径")
    return root


def _management_dir(project_root: Path) -> Path:
    path = project_root / ".project-to-act"
    _reject_link_or_reparse_point(path, "管理路径")
    if path.exists() and not path.is_dir():
        raise OSError(f"管理路径不是目录：{path}")
    return path


def _ensure_safe_directory(base: Path, target: Path) -> None:
    base_resolved = base.resolve()
    target_resolved = target.resolve(strict=False)
    try:
        relative = target_resolved.relative_to(base_resolved)
    except ValueError as error:
        raise ValueError(f"目录必须位于受控路径内：{target}") from error
    current = base_resolved
    _reject_link_or_reparse_point(current, "受控目录")
    for part in relative.parts:
        current = current / part
        _reject_link_or_reparse_point(current, "受控目录")
        if current.exists():
            if not current.is_dir():
                raise OSError(f"受控目录路径不是目录：{current}")
        else:
            current.mkdir()


def _safe_relative_file(project_root: Path, value: str | Path, label: str) -> tuple[Path, str]:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else project_root / raw
    _reject_link_or_reparse_point(candidate, label)
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(project_root)
    except ValueError as error:
        raise ValueError(f"{label}必须位于项目根目录内：{resolved}") from error
    if not resolved.is_file():
        raise ValueError(f"{label}不存在或不是常规文件：{resolved}")
    return resolved, relative.as_posix()


def _safe_management_path(management_dir: Path, value: str | Path, label: str) -> Path:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else management_dir / raw
    _reject_link_or_reparse_point(candidate, label)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(management_dir.resolve())
    except ValueError as error:
        raise ValueError(f"{label}必须位于管理目录内：{resolved}") from error
    return resolved


def _template_payloads() -> list[tuple[str, bytes]]:
    template_dir = Path(__file__).resolve().parent.parent / "assets" / "templates"
    payloads: list[tuple[str, bytes]] = []
    for filename in TEMPLATE_NAMES:
        source = template_dir / filename
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"缺少或不是常规文件的 Skill 模板：{source}")
        payloads.append((filename, source.read_bytes()))
    return payloads


def _copy_policy(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    result = json.loads(json.dumps(DEFAULT_POLICY))
    if policy:
        for key, value in policy.items():
            if key == "size_warning_bytes" and isinstance(value, dict):
                result[key].update(value)
            else:
                result[key] = value
    _validate_policy(result)
    return result


def _validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy.get("history_keep"), int) or policy["history_keep"] < 0:
        raise ValueError("policy.history_keep 必须是非负整数")
    if (
        not isinstance(policy.get("evidence_keep_unreferenced"), int)
        or policy["evidence_keep_unreferenced"] < 0
    ):
        raise ValueError("policy.evidence_keep_unreferenced 必须是非负整数")
    if policy.get("archive_partition") != "monthly":
        raise ValueError("当前只支持 policy.archive_partition=monthly")
    budgets = policy.get("size_warning_bytes")
    if not isinstance(budgets, dict):
        raise ValueError("policy.size_warning_bytes 必须是对象")
    for filename in TEMPLATE_NAMES:
        if not isinstance(budgets.get(filename), int) or budgets[filename] <= 0:
            raise ValueError(f"缺少有效体量阈值：{filename}")


def _copy_feature_reconcile(config: dict[str, Any] | None = None) -> dict[str, Any]:
    result = json.loads(json.dumps(DEFAULT_FEATURE_RECONCILE))
    if config:
        result.update(config)
    for key in ("expected_sources", "implemented_sources"):
        values = result.get(key)
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f"feature_reconcile.{key} 必须是非空字符串数组")
        normalized = [Path(item).as_posix() for item in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"feature_reconcile.{key} 不得包含重复路径")
        for item in normalized:
            path = Path(item)
            if path.is_absolute() or any(character in item for character in "*?[]"):
                raise ValueError(f"功能来源必须是无通配符的项目相对文件：{item}")
        result[key] = normalized
    if not isinstance(result.get("max_files"), int) or result["max_files"] <= 0:
        raise ValueError("feature_reconcile.max_files 必须是正整数")
    if not isinstance(result.get("max_total_bytes"), int) or result["max_total_bytes"] <= 0:
        raise ValueError("feature_reconcile.max_total_bytes 必须是正整数")
    unique_sources = set(result["expected_sources"]) | set(result["implemented_sources"])
    if len(unique_sources) > result["max_files"]:
        raise ValueError("配置的功能来源文件数超过 feature_reconcile.max_files")
    return result


def _config_payload(mode: str, canonical_ledger: str | None = None) -> bytes:
    config: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "mode": mode}
    if mode == "managed":
        config["policy"] = _copy_policy()
        config["feature_reconcile"] = _copy_feature_reconcile()
    if canonical_ledger is not None:
        config["canonical_ledger"] = canonical_ledger
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_config(config_path: Path) -> dict[str, Any]:
    _reject_link_or_reparse_point(config_path, "配置文件")
    if not config_path.is_file():
        raise OSError(f"配置路径不是常规文件：{config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"配置文件不是有效 UTF-8 JSON：{config_path}") from error
    if not isinstance(config, dict):
        raise ValueError(f"配置文件顶层必须是对象：{config_path}")
    schema = config.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"不支持的项目管理 schema：{schema!r}，当前支持 1 和 2")
    if config.get("mode") not in {"managed", "external-ledger"}:
        raise ValueError(f"未知项目管理模式：{config.get('mode')!r}")
    if schema == 2 and config["mode"] == "managed":
        config["policy"] = _copy_policy(config.get("policy"))
        config["feature_reconcile"] = _copy_feature_reconcile(config.get("feature_reconcile"))
    return config


def _policy_for_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("schema_version") == 2 and config.get("mode") == "managed":
        return _copy_policy(config.get("policy"))
    return _copy_policy()


def _redirect_target(project_root: Path, ledger_path: Path) -> str | None:
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    if not lines:
        return None
    first = lines[0].strip()
    if not first.startswith(REDIRECT_PREFIX) or not first.endswith("-->"):
        return None
    target = first[len(REDIRECT_PREFIX) : -3].strip()
    try:
        _, relative = _safe_relative_file(project_root, target, "账本跳转目标")
    except (ValueError, OSError):
        return None
    return relative if relative.startswith(".project-to-act/") else None


def _detect_existing_ledgers(project_root: Path) -> tuple[list[str], dict[str, str]]:
    ledgers: list[str] = []
    redirects: dict[str, str] = {}
    for relative in EXISTING_LEDGER_CANDIDATES:
        candidate = project_root / relative
        if candidate.exists() or candidate.is_symlink() or _is_windows_reparse_point(candidate):
            _, safe_relative = _safe_relative_file(project_root, relative, "现有项目账本")
            redirect = _redirect_target(project_root, candidate)
            if redirect is None:
                ledgers.append(safe_relative)
            else:
                redirects[safe_relative] = redirect
    return ledgers, redirects


def inspect_project(project_root: Path) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    management = _management_dir(root)
    external_ledgers, redirects = _detect_existing_ledgers(root)
    if not management.exists():
        return {
            "configured": False,
            "mode": "unconfigured",
            "external_ledgers": external_ledgers,
            "redirects": redirects,
            "action": "adopt-ledger" if external_ledgers else "initialize",
        }
    config_path = management / CONFIG_NAME
    _reject_link_or_reparse_point(config_path, "配置文件")
    if not config_path.exists():
        existing = [name for name in TEMPLATE_NAMES if (management / name).exists()]
        return {
            "configured": False,
            "mode": "legacy-managed" if existing else "invalid-empty",
            "external_ledgers": external_ledgers,
            "redirects": redirects,
            "existing_templates": existing,
            "missing_templates": [name for name in TEMPLATE_NAMES if name not in existing],
            "action": "migrate" if existing else "repair-or-remove-empty-directory",
        }
    config = _read_config(config_path)
    report: dict[str, Any] = {
        "configured": True,
        "schema_version": config["schema_version"],
        "mode": config["mode"],
        "external_ledgers": external_ledgers,
        "redirects": redirects,
    }
    if config["mode"] == "managed":
        report["missing_templates"] = [
            name for name in TEMPLATE_NAMES if not (management / name).is_file()
        ]
        report["policy"] = _policy_for_config(config)
        report["feature_reconcile"] = _copy_feature_reconcile(config.get("feature_reconcile"))
    else:
        canonical = config.get("canonical_ledger")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("external-ledger 模式缺少 canonical_ledger")
        _, canonical_relative = _safe_relative_file(root, canonical, "规范项目账本")
        report["canonical_ledger"] = canonical_relative
    return report


def _atomic_write(
    destination: Path,
    content: bytes,
    *,
    expected_hash: str | None = None,
    create_only: bool = False,
) -> None:
    _reject_link_or_reparse_point(destination, "目标文件")
    current = destination.read_bytes() if destination.exists() else b""
    if destination.exists() and not destination.is_file():
        raise OSError(f"目标路径不是常规文件：{destination}")
    if create_only and destination.exists():
        raise FileExistsError(f"目标文件已经存在：{destination}")
    if expected_hash is not None and _sha256_bytes(current) != expected_hash:
        raise RuntimeError(f"并发冲突，文件已变化：{destination}")
    _ensure_safe_directory(destination.parent.parent if destination.parent.name == ".project-to-act" else destination.parents[1], destination.parent)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _exclusive_write(destination: Path, content: bytes) -> bool:
    _reject_link_or_reparse_point(destination, "目标文件")
    if destination.exists():
        if not destination.is_file():
            raise OSError(f"目标路径不是常规文件：{destination}")
        return False
    try:
        with destination.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return False
    return True


def _write_managed_files(
    management: Path,
    templates: list[tuple[str, bytes]],
    *,
    dry_run: bool,
    include_config: bool,
) -> dict[str, Any]:
    planned = ([CONFIG_NAME] if include_config else []) + [
        filename for filename, _ in templates if not (management / filename).exists()
    ]
    if dry_run:
        return {"mode": "managed", "schema_version": 2, "dry_run": True, "created": planned, "skipped": []}
    if not management.exists():
        management.mkdir()
    _reject_link_or_reparse_point(management, "管理路径")
    created: list[str] = []
    skipped: list[str] = []
    if include_config:
        (created if _exclusive_write(management / CONFIG_NAME, _config_payload("managed")) else skipped).append(CONFIG_NAME)
    for filename, payload in templates:
        (created if _exclusive_write(management / filename, payload) else skipped).append(filename)
    return {"mode": "managed", "schema_version": 2, "dry_run": False, "created": created, "skipped": skipped}


def initialize(project_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    inspection = inspect_project(root)
    if inspection["mode"] == "external-ledger":
        raise ValueError("项目已采用现有账本，拒绝创建第二套管理文档")
    if inspection["mode"] == "legacy-managed":
        raise ValueError("检测到旧版 .project-to-act，请先使用 --migrate")
    if inspection["mode"] == "invalid-empty":
        raise ValueError("检测到空的 .project-to-act，请人工确认后修复或移除")
    if inspection.get("external_ledgers"):
        raise ValueError("检测到现有项目账本；请采用唯一账本，拒绝创建第二事实源")
    return _write_managed_files(
        _management_dir(root),
        _template_payloads(),
        dry_run=dry_run,
        include_config=not inspection.get("configured", False),
    )


def adopt_ledger(project_root: Path, ledger: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    _, relative = _safe_relative_file(root, ledger, "规范项目账本")
    inspection = inspect_project(root)
    if inspection["configured"]:
        if inspection["mode"] == "external-ledger" and inspection.get("canonical_ledger") == relative:
            return {"mode": "external-ledger", "schema_version": inspection["schema_version"], "dry_run": dry_run, "canonical_ledger": relative, "created": [], "skipped": [CONFIG_NAME]}
        raise ValueError("项目已有不同的 project-to-act 配置，拒绝改写")
    if inspection["mode"] != "unconfigured":
        raise ValueError("已有 .project-to-act 内容；请先验证或迁移，拒绝覆盖")
    detected = inspection["external_ledgers"]
    if len(detected) > 1 or (detected and relative not in detected):
        raise ValueError("检测到多个或不一致的现有账本；请先确认唯一事实源")
    result = {"mode": "external-ledger", "schema_version": 2, "dry_run": dry_run, "canonical_ledger": relative, "created": [CONFIG_NAME], "skipped": []}
    if dry_run:
        return result
    management = _management_dir(root)
    management.mkdir()
    _exclusive_write(management / CONFIG_NAME, _config_payload("external-ledger", relative))
    return result


def migrate_project(project_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    inspection = inspect_project(root)
    management = _management_dir(root)
    if inspection["mode"] == "legacy-managed":
        if inspection["external_ledgers"]:
            raise ValueError("旧版管理目录与外部账本并存；请先确认唯一事实源")
        return _write_managed_files(management, _template_payloads(), dry_run=dry_run, include_config=True)
    if not inspection["configured"]:
        raise ValueError("未检测到可迁移的项目管理配置")
    if inspection["schema_version"] == 2:
        return {"mode": inspection["mode"], "schema_from": 2, "schema_to": 2, "dry_run": dry_run, "created": [], "updated": [], "skipped": [CONFIG_NAME]}
    config_path = management / CONFIG_NAME
    old = _read_config(config_path)
    canonical = old.get("canonical_ledger") if old["mode"] == "external-ledger" else None
    result = {"mode": old["mode"], "schema_from": 1, "schema_to": 2, "dry_run": dry_run, "created": [], "updated": [CONFIG_NAME], "skipped": [], "content_relocated": False}
    if dry_run:
        return result
    before = config_path.read_bytes()
    _atomic_write(config_path, _config_payload(old["mode"], canonical), expected_hash=_sha256_bytes(before))
    return result


def _issue(code: str, message: str, *, file: str | None = None, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if file:
        item["file"] = file
    item.update(details)
    return item


def _h2_headings(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.startswith("## ")]


def _section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    target = f"## {heading}"
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == target]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def _table_rows(lines: list[str], span: tuple[int, int]) -> list[tuple[int, str]]:
    start, end = span
    header_index: int | None = None
    for index in range(start + 1, end - 1):
        if lines[index].lstrip().startswith("|") and TABLE_SEPARATOR_RE.match(lines[index + 1].rstrip("\r\n")):
            header_index = index
            break
    if header_index is None:
        return []
    rows: list[tuple[int, str]] = []
    for index in range(header_index + 2, end):
        stripped = lines[index].strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            rows.append((index, lines[index]))
        elif stripped:
            break
    return rows


def _first_table_cell(row: str) -> str:
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    return cells[0] if cells else ""


def _markdown_cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _table_records(lines: list[str], span: tuple[int, int]) -> list[dict[str, str]]:
    start, end = span
    header_index: int | None = None
    for index in range(start + 1, end - 1):
        if lines[index].lstrip().startswith("|") and TABLE_SEPARATOR_RE.match(lines[index + 1].rstrip("\r\n")):
            header_index = index
            break
    if header_index is None:
        return []
    headers = _markdown_cells(lines[header_index])
    records: list[dict[str, str]] = []
    for index in range(header_index + 2, end):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if not stripped.startswith("|") or not stripped.endswith("|"):
            break
        cells = _markdown_cells(lines[index])
        cells.extend([""] * (len(headers) - len(cells)))
        records.append(dict(zip(headers, cells, strict=False)))
    return records


def _audit_managed(root: Path, inspection: dict[str, Any]) -> dict[str, Any]:
    management = _management_dir(root)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    texts: dict[str, str] = {}
    policy = inspection["policy"]
    if inspection["external_ledgers"]:
        errors.append(_issue("MULTIPLE_FACT_SOURCES", "managed 模式存在外部账本", ledgers=inspection["external_ledgers"]))
    if inspection["schema_version"] == 1:
        warnings.append(_issue("SCHEMA_V1", "项目仍使用 schema v1；显式迁移后才能使用安全整理"))
    for filename in TEMPLATE_NAMES:
        path = management / filename
        if not path.is_file():
            errors.append(_issue("MISSING_DOCUMENT", "缺少管理文件", file=filename))
            continue
        try:
            _reject_link_or_reparse_point(path, "管理文件")
            payload = path.read_bytes()
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(_issue("INVALID_UTF8", "管理文件不是有效 UTF-8", file=filename))
            continue
        except OSError as error:
            errors.append(_issue("UNSAFE_DOCUMENT", str(error), file=filename))
            continue
        texts[filename] = text
        headings = _h2_headings(text)
        metrics[filename] = {"bytes": len(payload), "lines": len(text.splitlines()), "h2_count": len(headings)}
        spec = DOC_SPECS[filename]
        for required in spec["required"]:
            count = headings.count(required)
            if count == 0:
                errors.append(_issue("MISSING_HEADING", f"缺少标题：{required}", file=filename))
            elif count > 1:
                errors.append(_issue("DUPLICATE_HEADING", f"标题重复 {count} 次：{required}", file=filename))
        duplicate_h2 = sorted({heading for heading in headings if headings.count(heading) > 1})
        for heading in duplicate_h2:
            if heading not in spec["required"]:
                warnings.append(_issue("DUPLICATE_CUSTOM_HEADING", f"自定义标题重复：{heading}", file=filename))
        unexpected = [heading for heading in headings if heading not in spec["allowed"]]
        if unexpected:
            warnings.append(_issue("UNEXPECTED_H2", "存在职责范围外的二级标题", file=filename, headings=unexpected[:30], omitted=max(0, len(unexpected) - 30)))
        limit = policy["size_warning_bytes"][filename]
        if len(payload) > limit:
            warnings.append(_issue("SIZE_BUDGET_EXCEEDED", f"活动文档超过 {limit} 字节告警阈值", file=filename, bytes=len(payload), limit=limit))
        if "```" in text:
            warnings.append(_issue("EMBEDDED_CODE_BLOCK", "管理文档包含代码块，建议改为外部制品引用", file=filename))
        if RAW_OUTPUT_RE.search(text):
            warnings.append(_issue("RAW_OUTPUT_CANDIDATE", "检测到疑似原始日志或工具输出", file=filename))
        long_cells = 0
        for line in text.splitlines():
            if line.strip().startswith("|"):
                long_cells += sum(1 for cell in line.strip().strip("|").split("|") if len(cell.strip()) > 500)
        if long_cells:
            warnings.append(_issue("LONG_TABLE_CELL", "存在超过 500 字符的表格单元", file=filename, count=long_cells))
        h3_count = sum(1 for line in text.splitlines() if line.startswith("### "))
        if h3_count > 20:
            warnings.append(_issue("EXCESSIVE_SUBSECTIONS", "活动文档包含过多三级章节", file=filename, count=h3_count))
        lines = text.splitlines(keepends=True)
        for section, prefix in spec["registries"]:
            span = _section_span(lines, section)
            if span is None:
                continue
            ids = [
                _first_table_cell(row)
                for _, row in _table_rows(lines, span)
                if _first_table_cell(row).startswith(prefix)
            ]
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            if duplicates:
                errors.append(_issue("DUPLICATE_CANONICAL_ID", f"规范清单存在重复 ID：{', '.join(duplicates[:20])}", file=filename))
        if filename == "PROJECT_FEATURES.md":
            feature_span = _section_span(lines, "功能清单")
            if feature_span:
                records = _table_records(lines, feature_span)
                missing_evidence: list[str] = []
                missing_acceptance: list[str] = []
                missing_source: list[str] = []
                blank_values = {"", "-", "无", "待记录", "未记录"}
                for record in records:
                    feature_id = record.get("功能 ID", "")
                    if not feature_id.startswith("F-"):
                        continue
                    if "来源引用" in record and record.get("来源引用", "") in blank_values:
                        missing_source.append(feature_id)
                    if record.get("状态") == "已完成":
                        if record.get("证据 ID", "") in blank_values:
                            missing_evidence.append(feature_id)
                        if "验收状态" in record and record.get("验收状态") not in {"通过", "已通过"}:
                            missing_acceptance.append(feature_id)
                if missing_source:
                    warnings.append(_issue("FEATURE_SOURCE_MISSING", "功能缺少来源引用", file=filename, ids=missing_source[:30]))
                if missing_evidence:
                    warnings.append(_issue("COMPLETED_FEATURE_WITHOUT_EVIDENCE", "已完成功能缺少证据 ID", file=filename, ids=missing_evidence[:30]))
                if missing_acceptance:
                    warnings.append(_issue("COMPLETED_FEATURE_NOT_ACCEPTED", "已完成功能尚未通过验收", file=filename, ids=missing_acceptance[:30]))
        for reference in MARKDOWN_LINK_RE.findall(text):
            try:
                archive_path = _safe_management_path(management, reference, "归档引用")
            except (ValueError, OSError) as error:
                errors.append(_issue("UNSAFE_ARCHIVE_REFERENCE", str(error), file=filename, reference=reference))
                continue
            if not archive_path.is_file():
                errors.append(_issue("BROKEN_ARCHIVE_REFERENCE", "归档引用不存在", file=filename, reference=reference))
    extras = sorted(
        path.name
        for path in management.glob("PROJECT_*.md")
        if path.name not in TEMPLATE_NAMES
    )
    if extras:
        warnings.append(_issue("EXTRA_PROJECT_DOCUMENT", "管理根目录存在职责未定义的 PROJECT 文档", files=extras))
    acceptance = texts.get("PROJECT_ACCEPTANCE.md")
    if acceptance:
        lines = acceptance.splitlines(keepends=True)
        evidence_span = _section_span(lines, "证据索引")
        evidence_ids: set[str] = set()
        evidence_range: set[int] = set()
        if evidence_span:
            evidence_range = set(range(*evidence_span))
            evidence_ids = {
                _first_table_cell(row)
                for _, row in _table_rows(lines, evidence_span)
                if _first_table_cell(row).startswith("E-")
            }
        references: set[str] = set()
        for filename, text in texts.items():
            source_lines = text.splitlines(keepends=True)
            for index, line in enumerate(source_lines):
                if filename == "PROJECT_ACCEPTANCE.md" and index in evidence_range:
                    continue
                references.update(token for token in ID_RE.findall(line) if token.startswith("E-"))
        missing = sorted(reference for reference in references if reference not in evidence_ids and reference != "E-000")
        if missing:
            warnings.append(_issue("MISSING_EVIDENCE_REFERENCE", "存在未登记的证据 ID 引用", file="PROJECT_ACCEPTANCE.md", ids=missing[:30], omitted=max(0, len(missing) - 30)))
    return {"errors": errors, "warnings": warnings, "metrics": metrics}


def audit_project(project_root: Path, *, strict: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    try:
        inspection = inspect_project(root)
    except (ValueError, OSError) as error:
        errors.append(_issue("INVALID_CONFIGURATION", str(error)))
        return {"valid": False, "strict_valid": False, "mode": "invalid", "errors": errors, "warnings": warnings, "metrics": metrics}
    if not inspection["configured"]:
        errors.append(_issue("UNCONFIGURED", f"项目管理尚未配置；建议操作：{inspection['action']}"))
        return {"valid": False, "strict_valid": False, "mode": inspection["mode"], "errors": errors, "warnings": warnings, "metrics": metrics}
    if inspection["mode"] == "managed":
        result = _audit_managed(root, inspection)
        errors.extend(result["errors"])
        warnings.extend(result["warnings"])
        metrics.update(result["metrics"])
    else:
        if inspection["schema_version"] == 1:
            warnings.append(_issue("SCHEMA_V1", "项目仍使用 schema v1"))
        noncanonical = [item for item in inspection["external_ledgers"] if item != inspection["canonical_ledger"]]
        if noncanonical:
            errors.append(_issue("MULTIPLE_FACT_SOURCES", "存在非规范账本", ledgers=noncanonical))
        ledger = root / inspection["canonical_ledger"]
        try:
            payload = ledger.read_bytes()
            text = payload.decode("utf-8")
            headings = _h2_headings(text)
            metrics[inspection["canonical_ledger"]] = {"bytes": len(payload), "lines": len(text.splitlines()), "h2_count": len(headings)}
            if len(headings) < 2:
                errors.append(_issue("INSUFFICIENT_HEADINGS", "规范外部账本至少需要两个二级标题", file=inspection["canonical_ledger"]))
        except UnicodeDecodeError:
            errors.append(_issue("INVALID_UTF8", "规范外部账本不是有效 UTF-8", file=inspection["canonical_ledger"]))
    valid = not errors
    strict_valid = valid and (not strict or not warnings)
    return {
        "valid": valid,
        "strict_valid": strict_valid,
        "schema_version": inspection["schema_version"],
        "mode": inspection["mode"],
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
    }


def validate_project(project_root: Path, *, strict: bool = False) -> dict[str, Any]:
    result = audit_project(project_root, strict=strict)
    result["issues"] = [item["message"] for item in result["errors"]]
    return result


def _feature_source_paths(root: Path, config: dict[str, Any]) -> dict[str, list[tuple[Path, str]]]:
    resolved: dict[str, list[tuple[Path, str]]] = {"expected": [], "implemented": []}
    for role, key in (("expected", "expected_sources"), ("implemented", "implemented_sources")):
        for value in config[key]:
            path, relative = _safe_relative_file(root, value, f"{role} 功能来源")
            if relative.startswith(".project-to-act/"):
                raise ValueError(f"功能来源必须位于管理目录外：{relative}")
            resolved[role].append((path, relative))
    unique = {relative: path for entries in resolved.values() for path, relative in entries}
    total_bytes = sum(path.stat().st_size for path in unique.values())
    if total_bytes > config["max_total_bytes"]:
        raise ValueError(
            f"功能来源总量 {total_bytes} 字节超过上限 {config['max_total_bytes']} 字节"
        )
    return resolved


def _feature_input_fingerprint(
    management: Path,
    reconcile_config: dict[str, Any],
    sources: dict[str, list[tuple[Path, str]]],
) -> tuple[str, dict[str, Any]]:
    paths = {relative: path for entries in sources.values() for path, relative in entries}
    feature_path = management / "PROJECT_FEATURES.md"
    paths[".project-to-act/PROJECT_FEATURES.md"] = feature_path
    files: dict[str, dict[str, int]] = {}
    for relative, path in sorted(paths.items()):
        stat_result = path.stat()
        files[relative] = {"size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns}
    payload = {
        "version": FEATURE_RECONCILE_VERSION,
        "config": reconcile_config,
        "files": files,
    }
    signature = _sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return signature, payload


def _read_reconcile_state(state_path: Path) -> dict[str, Any] | None:
    _reject_link_or_reparse_point(state_path, "功能对账状态")
    if not state_path.exists():
        return None
    if not state_path.is_file():
        raise OSError(f"功能对账状态不是常规文件：{state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("功能对账状态不是有效 UTF-8 JSON") from error
    if not isinstance(state, dict) or state.get("version") != FEATURE_RECONCILE_VERSION:
        return None
    return state


def _feature_ids(text: str) -> set[str]:
    return {token for token in ID_RE.findall(text) if token.startswith("F-")}


def _limited_ids(ids: set[str]) -> dict[str, Any]:
    ordered = sorted(ids)
    return {"count": len(ordered), "ids": ordered[:100], "omitted": max(0, len(ordered) - 100)}


def reconcile_features(
    project_root: Path,
    *,
    strict: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    inspection = inspect_project(root)
    if not inspection["configured"] or inspection["mode"] != "managed":
        raise ValueError("功能对账只支持已配置的 managed 模式")
    if inspection["schema_version"] != 2:
        raise ValueError("功能对账需要 schema v2")
    reconcile_config = inspection["feature_reconcile"]
    if not reconcile_config["expected_sources"] and not reconcile_config["implemented_sources"]:
        warning = _issue("FEATURE_RECONCILE_NOT_CONFIGURED", "未配置功能来源；未启动任何搜索")
        return {
            "status": "not_configured",
            "reason": "no_sources",
            "valid": True,
            "strict_valid": not strict,
            "warnings": [warning],
            "source_files_read": 0,
            "state_written": False,
        }
    management = _management_dir(root)
    sources = _feature_source_paths(root, reconcile_config)
    state_path = _safe_management_path(management, FEATURE_RECONCILE_STATE, "功能对账状态")
    lock_path = _safe_management_path(management, FEATURE_RECONCILE_LOCK, "功能对账锁")
    _ensure_safe_directory(management, lock_path.parent)
    try:
        lock_stream = lock_path.open("xb")
    except FileExistsError as error:
        raise RuntimeError(f"功能对账已在运行；如确认没有进程，请人工检查锁文件：{lock_path}") from error
    try:
        with lock_stream:
            lock_stream.write((json.dumps({"pid": os.getpid(), "started_at": _now_iso()}) + "\n").encode("utf-8"))
        signature, fingerprint = _feature_input_fingerprint(management, reconcile_config, sources)
        state = _read_reconcile_state(state_path)
        if state and state.get("signature") == signature and state.get("status") == "complete":
            cached = dict(state["result"])
            cached.update(
                {
                    "status": "skipped",
                    "reason": "inputs_unchanged",
                    "cache_hit": True,
                    "source_files_read": 0,
                    "state_written": False,
                }
            )
            cached["strict_valid"] = cached.get("valid", True) and (
                not strict or not cached.get("warnings")
            )
            return cached

        payloads: dict[str, bytes] = {}
        for relative, source_path in sorted(
            {relative: path for entries in sources.values() for path, relative in entries}.items()
        ):
            payloads[relative] = source_path.read_bytes()
            try:
                payloads[relative].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"功能来源不是有效 UTF-8：{relative}") from error
        after_signature, _ = _feature_input_fingerprint(management, reconcile_config, sources)
        if after_signature != signature:
            raise RuntimeError("功能来源在对账期间发生变化；已停止并拒绝写入缓存")

        role_ids: dict[str, set[str]] = {"expected": set(), "implemented": set()}
        content_hashes: dict[str, str] = {}
        for role, entries in sources.items():
            for _, relative in entries:
                payload = payloads[relative]
                role_ids[role].update(_feature_ids(payload.decode("utf-8")))
                content_hashes[relative] = _sha256_bytes(payload)

        feature_text = (management / "PROJECT_FEATURES.md").read_bytes().decode("utf-8")
        feature_lines = feature_text.splitlines(keepends=True)
        feature_span = _section_span(feature_lines, "功能清单")
        if feature_span is None:
            raise ValueError("PROJECT_FEATURES.md 缺少唯一的功能清单")
        records = _table_records(feature_lines, feature_span)
        registered: dict[str, dict[str, str]] = {
            record.get("功能 ID", ""): record
            for record in records
            if record.get("功能 ID", "").startswith("F-")
        }
        registered_ids = set(registered)
        expected_ids = role_ids["expected"]
        implemented_ids = role_ids["implemented"]
        differences = {
            "expected_unregistered": _limited_ids(expected_ids - registered_ids),
            "implemented_unregistered": _limited_ids(implemented_ids - registered_ids),
            "expected_not_implemented": _limited_ids(expected_ids - implemented_ids)
            if expected_ids and implemented_ids
            else _limited_ids(set()),
            "registered_not_in_expected_sources": _limited_ids(registered_ids - expected_ids)
            if expected_ids
            else _limited_ids(set()),
        }
        blank_values = {"", "-", "无", "待记录", "未记录"}
        completed_without_evidence = {
            feature_id
            for feature_id, record in registered.items()
            if record.get("状态") == "已完成" and record.get("证据 ID", "") in blank_values
        }
        completed_not_accepted = {
            feature_id
            for feature_id, record in registered.items()
            if record.get("状态") == "已完成"
            and "验收状态" in record
            and record.get("验收状态") not in {"通过", "已通过"}
        }
        differences["completed_without_evidence"] = _limited_ids(completed_without_evidence)
        differences["completed_not_accepted"] = _limited_ids(completed_not_accepted)
        warning_map = (
            ("EXPECTED_FEATURE_UNREGISTERED", "预期来源中的功能未登记", "expected_unregistered"),
            ("IMPLEMENTED_FEATURE_UNREGISTERED", "实现来源中的功能未登记", "implemented_unregistered"),
            ("EXPECTED_FEATURE_NOT_IMPLEMENTED", "预期功能未出现在实现来源", "expected_not_implemented"),
            (
                "REGISTERED_FEATURE_NOT_EXPECTED",
                "功能清单中的功能未出现在预期来源",
                "registered_not_in_expected_sources",
            ),
            ("COMPLETED_FEATURE_WITHOUT_EVIDENCE", "已完成功能缺少证据", "completed_without_evidence"),
            ("COMPLETED_FEATURE_NOT_ACCEPTED", "已完成功能尚未通过验收", "completed_not_accepted"),
        )
        warnings = [
            _issue(code, message, ids=differences[key]["ids"], count=differences[key]["count"])
            for code, message, key in warning_map
            if differences[key]["count"]
        ]
        result = {
            "status": "complete",
            "reason": "inputs_changed_or_first_run",
            "cache_hit": False,
            "signature": signature,
            "valid": True,
            "strict_valid": not strict or not warnings,
            "warnings": warnings,
            "source_files_read": len(payloads),
            "state_written": not dry_run,
            "counts": {
                "registered": len(registered_ids),
                "expected": len(expected_ids),
                "implemented": len(implemented_ids),
            },
            "differences": differences,
        }
        if not dry_run:
            state_payload = {
                "version": FEATURE_RECONCILE_VERSION,
                "status": "complete",
                "signature": signature,
                "completed_at": _now_iso(),
                "fingerprint": fingerprint,
                "content_sha256": content_hashes,
                "result": result,
            }
            old = state_path.read_bytes() if state_path.exists() else b""
            _atomic_write(
                state_path,
                (json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                expected_hash=_sha256_bytes(old),
            )
        return result
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _record_month(text: str) -> str:
    match = DATE_MONTH_RE.search(text)
    return f"{match.group(1)}.md" if match else "legacy-undated.md"


def _parse_records(lines: list[str], span: tuple[int, int]) -> tuple[list[dict[str, Any]], str | None]:
    start, end = span
    table_rows = _table_rows(lines, span)
    if table_rows:
        records = [{"start": index, "end": index + 1, "text": row, "format": "table"} for index, row in table_rows]
        trailing_non_table = [
            lines[index].strip()
            for index in range(table_rows[-1][0] + 1, end)
            if lines[index].strip()
        ]
        return records, "mixed content after table" if trailing_non_table else None
    for index in range(start + 1, end - 1):
        if lines[index].lstrip().startswith("|") and TABLE_SEPARATOR_RE.match(lines[index + 1].rstrip("\r\n")):
            trailing = [line.strip() for line in lines[index + 2 : end] if line.strip()]
            return [], "mixed content after empty table" if trailing else None
    h3 = [index for index in range(start + 1, end) if lines[index].startswith("### ")]
    if h3:
        records = []
        for position, index in enumerate(h3):
            record_end = h3[position + 1] if position + 1 < len(h3) else end
            records.append({"start": index, "end": record_end, "text": "".join(lines[index:record_end]), "format": "subsection"})
        return records, None
    bullets = [index for index in range(start + 1, end) if lines[index].startswith("- ")]
    if bullets:
        records = []
        for position, index in enumerate(bullets):
            record_end = bullets[position + 1] if position + 1 < len(bullets) else end
            records.append({"start": index, "end": record_end, "text": "".join(lines[index:record_end]), "format": "bullet"})
        return records, None
    substantive = [line for line in lines[start + 1 : end] if line.strip()]
    return [], "unrecognized history format" if substantive else None


def _plan_document_compaction(
    filename: str,
    text: str,
    *,
    history_keep: int,
    evidence_keep: int,
) -> dict[str, Any]:
    lines = text.splitlines(keepends=True)
    removals: list[dict[str, Any]] = []
    manual: list[dict[str, str]] = []
    spec = DOC_SPECS[filename]
    for heading in spec["history"]:
        span = _section_span(lines, heading)
        if span is None:
            continue
        records, reason = _parse_records(lines, span)
        if reason:
            manual.append({"section": heading, "reason": reason})
        for record in records[history_keep:]:
            removals.append({**record, "section": heading, "partition": _record_month(record["text"])})
    if filename == "PROJECT_ACCEPTANCE.md":
        evidence_span = _section_span(lines, "证据索引")
        if evidence_span:
            records, reason = _parse_records(lines, evidence_span)
            if reason:
                manual.append({"section": "证据索引", "reason": reason})
            outside = "".join(line for index, line in enumerate(lines) if not (evidence_span[0] <= index < evidence_span[1]))
            referenced = {item for item in ID_RE.findall(outside) if item.startswith("E-")}
            unreferenced_seen = 0
            for record in records:
                evidence_id = _first_table_cell(record["text"])
                if evidence_id in referenced:
                    continue
                unreferenced_seen += 1
                if unreferenced_seen > evidence_keep:
                    removals.append({**record, "section": "证据索引", "partition": _record_month(record["text"])})
        gate_span = _section_span(lines, "Gate 记录")
        if gate_span:
            records, reason = _parse_records(lines, gate_span)
            if reason:
                manual.append({"section": "Gate 记录", "reason": reason})
            for record in records[history_keep:]:
                removals.append({**record, "section": "Gate 记录", "partition": _record_month(record["text"])})
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for item in removals:
        unique[(item["start"], item["end"])] = item
    removals = sorted(unique.values(), key=lambda item: item["start"])
    remove_lines = {index for item in removals for index in range(item["start"], item["end"])}
    domain = DOC_SPECS[filename]["domain"]
    section_links: dict[str, set[str]] = {}
    section_starts: dict[int, str] = {}
    for item in removals:
        section_links.setdefault(item["section"], set()).add(
            f"archive/{domain}/{item['partition']}"
        )
    for section in section_links:
        span = _section_span(lines, section)
        if span:
            section_starts[span[0]] = section
            for index in range(span[0] + 1, span[1]):
                if lines[index].startswith("> 历史归档："):
                    section_links[section].update(MARKDOWN_LINK_RE.findall(lines[index]))
    output: list[str] = []
    for index, line in enumerate(lines):
        if index in remove_lines or line.startswith("> 历史归档：") and any(
            span and span[0] < index < span[1]
            for span in (_section_span(lines, section) for section in section_links)
        ):
            continue
        output.append(line)
        section = section_starts.get(index)
        if section:
            links = "、".join(
                f"[{Path(reference).name.removesuffix('.md')}]({reference})"
                for reference in sorted(section_links[section])
            )
            output.append(f"\n> 历史归档：{links}\n")
    post_text = "".join(output)
    return {"filename": filename, "pre_text": text, "post_text": post_text, "records": removals, "manual_review": manual}


def build_compaction_plan(project_root: Path) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    inspection = inspect_project(root)
    if not inspection["configured"] or inspection["mode"] != "managed":
        raise ValueError("整理只支持已配置的 managed 模式")
    if inspection["schema_version"] != 2:
        raise ValueError("整理需要 schema v2；请先预览并执行迁移")
    audit = audit_project(root)
    if audit["errors"]:
        raise ValueError("项目存在结构错误，修复后才能整理：" + "；".join(item["message"] for item in audit["errors"][:5]))
    management = _management_dir(root)
    policy = inspection["policy"]
    documents: list[dict[str, Any]] = []
    operation_seed: list[str] = []
    manual: list[dict[str, str]] = []
    for filename in TEMPLATE_NAMES:
        text = (management / filename).read_bytes().decode("utf-8")
        plan = _plan_document_compaction(
            filename,
            text,
            history_keep=policy["history_keep"],
            evidence_keep=policy["evidence_keep_unreferenced"],
        )
        if plan["manual_review"]:
            manual.extend({"file": filename, **item} for item in plan["manual_review"])
        if plan["records"]:
            pre_bytes = plan["pre_text"].encode("utf-8")
            post_bytes = plan["post_text"].encode("utf-8")
            plan["pre_sha256"] = _sha256_bytes(pre_bytes)
            plan["post_sha256"] = _sha256_bytes(post_bytes)
            for record in plan["records"]:
                record["sha256"] = _sha256_bytes(record["text"].encode("utf-8"))
                operation_seed.append(f"{filename}:{record['start']}:{record['sha256']}")
            operation_seed.append(plan["pre_sha256"])
            documents.append(plan)
    operation_id = hashlib.sha256("\n".join(operation_seed).encode("utf-8")).hexdigest()[:24] if operation_seed else None
    archive_groups: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        domain = DOC_SPECS[document["filename"]]["domain"]
        for record in document["records"]:
            relative = f"archive/{domain}/{record['partition']}"
            archive_groups.setdefault(relative, []).append(
                {
                    "source": document["filename"],
                    "section": record["section"],
                    "sha256": record["sha256"],
                    "text": record["text"],
                }
            )
    return {
        "operation_id": operation_id,
        "documents": documents,
        "archive_groups": archive_groups,
        "manual_review": manual,
        "audit_warnings": audit["warnings"],
    }


def _archive_addition(operation_id: str, relative: str, records: list[dict[str, Any]]) -> bytes:
    parts = [
        f"\n{ARCHIVE_MARKER_PREFIX}{operation_id} -->\n",
        f"## Compaction {operation_id}\n\n",
        f"- Archived at: {_now_iso()}\n",
        f"- Partition: `{relative}`\n\n",
    ]
    for record in records:
        parts.extend(
            [
                f"### {record['source']} / {record['section']} / {record['sha256'][:12]}\n\n",
                record["text"],
                "\n" if not record["text"].endswith("\n") else "",
                "\n",
            ]
        )
    return "".join(parts).encode("utf-8")


def compact_project(project_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    management = _management_dir(root)
    plan = build_compaction_plan(root)
    operation_id = plan["operation_id"]
    summary = {
        "mode": "managed",
        "schema_version": 2,
        "dry_run": dry_run,
        "changed": bool(operation_id),
        "operation_id": operation_id,
        "planned_documents": [
            {"file": item["filename"], "archived_records": len(item["records"]), "pre_sha256": item["pre_sha256"], "post_sha256": item["post_sha256"]}
            for item in plan["documents"]
        ],
        "archive_files": sorted(plan["archive_groups"]),
        "archived_records": sum(len(item["records"]) for item in plan["documents"]),
        "manual_review": plan["manual_review"],
        "manifest": None,
    }
    if dry_run or not operation_id:
        return summary
    for document in plan["documents"]:
        current = (management / document["filename"]).read_bytes()
        if _sha256_bytes(current) != document["pre_sha256"]:
            raise RuntimeError(f"并发冲突，整理前文件已变化：{document['filename']}")
    archive_preimages: dict[str, tuple[bytes, str]] = {}
    for relative, records in plan["archive_groups"].items():
        destination = _safe_management_path(management, relative, "归档文件")
        _ensure_safe_directory(management, destination.parent)
        old = destination.read_bytes() if destination.exists() else b""
        marker = f"{ARCHIVE_MARKER_PREFIX}{operation_id} -->".encode("utf-8")
        archive_preimages[relative] = (old, _sha256_bytes(old))
        if marker not in old:
            header = b"# Project-to-Act Archive\n" if not old else b""
            new = old + header + _archive_addition(operation_id, relative, records)
            _atomic_write(destination, new, expected_hash=_sha256_bytes(old))
    for document in plan["documents"]:
        current = (management / document["filename"]).read_bytes()
        if _sha256_bytes(current) != document["pre_sha256"]:
            raise RuntimeError(f"并发冲突，归档后源文件已变化：{document['filename']}")
    manifest_relative = f"archive/manifests/{operation_id}.json"
    manifest_path = _safe_management_path(management, manifest_relative, "整理清单")
    _ensure_safe_directory(management, manifest_path.parent)
    manifest: dict[str, Any] = {
        "format": "project-to-act-compaction-v2",
        "operation_id": operation_id,
        "created_at": _now_iso(),
        "status": "prepared",
        "documents": [],
        "archives": [
            {"path": relative, "pre_sha256": pre_hash}
            for relative, (_, pre_hash) in sorted(archive_preimages.items())
        ],
    }
    for document in plan["documents"]:
        compressed = zlib.compress(document["pre_text"].encode("utf-8"), level=9)
        manifest["documents"].append(
            {
                "path": document["filename"],
                "pre_sha256": document["pre_sha256"],
                "post_sha256": document["post_sha256"],
                "preimage_zlib_base64": base64.b64encode(compressed).decode("ascii"),
                "archived_records": len(document["records"]),
            }
        )
    prepared = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("operation_id") != operation_id:
            raise RuntimeError("整理清单路径冲突")
    else:
        _atomic_write(manifest_path, prepared, expected_hash=_sha256_bytes(b""))
    for document in plan["documents"]:
        destination = management / document["filename"]
        _atomic_write(destination, document["post_text"].encode("utf-8"), expected_hash=document["pre_sha256"])
    manifest["status"] = "complete"
    manifest["completed_at"] = _now_iso()
    complete = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    prepared_hash = _sha256_bytes(manifest_path.read_bytes())
    _atomic_write(manifest_path, complete, expected_hash=prepared_hash)
    summary["manifest"] = manifest_relative
    return summary


def restore_compaction(project_root: Path, manifest_value: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    management = _management_dir(root)
    manifest_path = _safe_management_path(management, manifest_value, "整理清单")
    if not manifest_path.is_file():
        raise ValueError(f"整理清单不存在：{manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("整理清单不是有效 UTF-8 JSON") from error
    if manifest.get("format") != "project-to-act-compaction-v2":
        raise ValueError("不支持的整理清单格式")
    actions: list[dict[str, Any]] = []
    decoded: list[tuple[Path, bytes, str, str]] = []
    for item in manifest.get("documents", []):
        destination = _safe_management_path(management, item["path"], "恢复目标")
        current = destination.read_bytes() if destination.exists() else b""
        current_hash = _sha256_bytes(current)
        pre_hash = item["pre_sha256"]
        post_hash = item["post_sha256"]
        if current_hash not in {pre_hash, post_hash}:
            raise RuntimeError(f"恢复冲突，当前文件既不是整理前也不是整理后版本：{item['path']}")
        try:
            preimage = zlib.decompress(base64.b64decode(item["preimage_zlib_base64"]))
        except (ValueError, zlib.error) as error:
            raise ValueError(f"恢复数据损坏：{item['path']}") from error
        if _sha256_bytes(preimage) != pre_hash:
            raise ValueError(f"恢复数据哈希不匹配：{item['path']}")
        action = "already-restored" if current_hash == pre_hash else "restore"
        actions.append({"file": item["path"], "action": action})
        decoded.append((destination, preimage, current_hash, post_hash))
    if not dry_run:
        for destination, preimage, current_hash, post_hash in decoded:
            if current_hash == post_hash:
                _atomic_write(destination, preimage, expected_hash=current_hash)
    return {
        "dry_run": dry_run,
        "operation_id": manifest.get("operation_id"),
        "restored": sum(1 for item in actions if item["action"] == "restore") if not dry_run else 0,
        "actions": actions,
        "archives_retained": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="安全发现、初始化、审计、迁移、整理和恢复项目治理文档。")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="明确的项目根目录；默认当前目录。")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="只检查现有管理模式。")
    actions.add_argument("--validate", action="store_true", help="验证配置、结构和归档引用。")
    actions.add_argument("--audit", action="store_true", help="输出职责、体量和内容卫生诊断。")
    actions.add_argument("--reconcile-features", action="store_true", help="只对账显式配置的功能来源文件。")
    actions.add_argument("--migrate", action="store_true", help="迁移旧版或 schema v1 配置，不搬移内容。")
    actions.add_argument("--adopt-ledger", metavar="PATH", help="采用项目内已有账本。")
    actions.add_argument("--compact", action="store_true", help="将明确历史记录移入分区 Markdown 归档。")
    actions.add_argument("--restore-compaction", metavar="MANIFEST", help="依据清单恢复整理前活动文档。")
    parser.add_argument("--dry-run", action="store_true", help="预览初始化、采用、迁移、整理或恢复，不写文件。")
    parser.add_argument("--strict", action="store_true", help="在审计、验证或功能对账时让告警也返回非零。")
    args = parser.parse_args()
    if args.strict and not (args.audit or args.validate or args.reconcile_features):
        parser.error("--strict 只能与 --audit、--validate 或 --reconcile-features 一起使用")
    try:
        if args.check:
            result = inspect_project(args.project_root)
        elif args.validate:
            result = validate_project(args.project_root, strict=args.strict)
        elif args.audit:
            result = audit_project(args.project_root, strict=args.strict)
        elif args.reconcile_features:
            result = reconcile_features(
                args.project_root, strict=args.strict, dry_run=args.dry_run
            )
        elif args.migrate:
            result = migrate_project(args.project_root, dry_run=args.dry_run)
        elif args.adopt_ledger:
            result = adopt_ledger(args.project_root, args.adopt_ledger, dry_run=args.dry_run)
        elif args.compact:
            result = compact_project(args.project_root, dry_run=args.dry_run)
        elif args.restore_compaction:
            result = restore_compaction(args.project_root, args.restore_compaction, dry_run=args.dry_run)
        else:
            result = initialize(args.project_root, dry_run=args.dry_run)
    except (ValueError, FileNotFoundError, OSError, RuntimeError) as error:
        parser.exit(1, f"操作失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False))
    if (args.validate or args.audit or args.reconcile_features) and not result["strict_valid"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
