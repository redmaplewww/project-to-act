#!/usr/bin/env python3
"""安全发现、初始化、采用和验证项目的持久管理文档。"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CONFIG_NAME = "PROJECT_CONFIG.json"
INIT_LOCK_NAME = ".project-to-act.init.lock"
INIT_LOCK_TIMEOUT_SECONDS = 5.0
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
REQUIRED_HEADINGS = {
    "PROJECT_OVERVIEW.md": ("## 项目目标", "## 范围", "## 当前焦点"),
    "PROJECT_PROGRESS.md": ("## 当前任务", "## 阻塞项", "## 进度历史"),
    "PROJECT_VERSIONS.md": ("## 当前版本", "## 版本历史"),
    "PROJECT_FEATURES.md": ("## 状态定义", "## 功能清单", "## 功能变更历史"),
    "PROJECT_ACCEPTANCE.md": ("## 当前验收结论", "## 验收标准", "## 验收记录"),
}


def _is_windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        file_attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(file_attributes & reparse_flag)


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
    return root


@contextmanager
def _initialization_lock(project_root: Path):
    lock_path = project_root / INIT_LOCK_NAME
    _reject_link_or_reparse_point(lock_path, "初始化锁")
    deadline = time.monotonic() + INIT_LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _reject_link_or_reparse_point(lock_path, "初始化锁")
            if time.monotonic() >= deadline:
                raise OSError(f"等待并发初始化超时；确认没有活动进程后移除：{lock_path}")
            time.sleep(0.01)
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _template_payloads() -> list[tuple[str, bytes]]:
    template_dir = Path(__file__).resolve().parent.parent / "assets" / "templates"
    templates: list[tuple[str, bytes]] = []
    for filename in TEMPLATE_NAMES:
        source = template_dir / filename
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"缺少或不是常规文件的 Skill 模板：{source}")
        with source.open("rb") as source_file:
            templates.append((filename, source_file.read()))
    return templates


def _management_dir(project_root: Path) -> Path:
    management_dir = project_root / ".project-to-act"
    _reject_link_or_reparse_point(management_dir, "管理路径")
    if management_dir.exists() and not management_dir.is_dir():
        raise OSError(f"管理路径不是目录：{management_dir}")
    return management_dir


def _safe_relative_file(project_root: Path, value: str | Path, label: str) -> tuple[Path, str]:
    raw_path = Path(value).expanduser()
    candidate = raw_path if raw_path.is_absolute() else project_root / raw_path
    _reject_link_or_reparse_point(candidate, label)
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(project_root)
    except ValueError as error:
        raise ValueError(f"{label}必须位于项目根目录内：{resolved}") from error
    if not resolved.is_file():
        raise ValueError(f"{label}不存在或不是常规文件：{resolved}")
    return resolved, relative.as_posix()


def _redirect_target(project_root: Path, ledger_path: Path) -> str | None:
    try:
        first_line = ledger_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (UnicodeDecodeError, IndexError):
        return None
    if not first_line.startswith(REDIRECT_PREFIX) or not first_line.endswith("-->"):
        return None
    target_value = first_line[len(REDIRECT_PREFIX) : -3].strip()
    try:
        _, relative = _safe_relative_file(project_root, target_value, "账本跳转目标")
    except (ValueError, OSError):
        return None
    if not relative.startswith(".project-to-act/"):
        return None
    return relative


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


def _config_payload(mode: str, canonical_ledger: str | None = None) -> bytes:
    config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
    }
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
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"不支持的项目管理 schema：{config.get('schema_version')!r}，"
            f"当前支持 {SCHEMA_VERSION}"
        )
    if config.get("mode") not in {"managed", "external-ledger"}:
        raise ValueError(f"未知项目管理模式：{config.get('mode')!r}")
    return config


def inspect_project(project_root: Path) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    management_dir = _management_dir(root)
    external_ledgers, redirects = _detect_existing_ledgers(root)
    if not management_dir.exists():
        return {
            "configured": False,
            "mode": "unconfigured",
            "external_ledgers": external_ledgers,
            "redirects": redirects,
            "action": "adopt-ledger" if external_ledgers else "initialize",
        }

    config_path = management_dir / CONFIG_NAME
    _reject_link_or_reparse_point(config_path, "配置文件")
    if not config_path.exists():
        existing_templates = [
            name
            for name in TEMPLATE_NAMES
            if (management_dir / name).exists()
            or (management_dir / name).is_symlink()
            or _is_windows_reparse_point(management_dir / name)
        ]
        return {
            "configured": False,
            "mode": "legacy-managed" if existing_templates else "invalid-empty",
            "external_ledgers": external_ledgers,
            "redirects": redirects,
            "existing_templates": existing_templates,
            "missing_templates": [name for name in TEMPLATE_NAMES if name not in existing_templates],
            "action": "migrate" if existing_templates else "repair-or-remove-empty-directory",
        }

    config = _read_config(config_path)
    mode = config["mode"]
    report: dict[str, Any] = {
        "configured": True,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "external_ledgers": external_ledgers,
        "redirects": redirects,
    }
    if mode == "managed":
        report["missing_templates"] = [
            name for name in TEMPLATE_NAMES if not (management_dir / name).is_file()
        ]
    else:
        canonical = config.get("canonical_ledger")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("external-ledger 模式缺少 canonical_ledger")
        _, canonical_relative = _safe_relative_file(root, canonical, "规范项目账本")
        report["canonical_ledger"] = canonical_relative
    return report


def _ensure_management_dir(management_dir: Path) -> None:
    try:
        management_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        _reject_link_or_reparse_point(management_dir, "管理路径")
        if not management_dir.is_dir():
            raise OSError(f"管理路径不是安全目录：{management_dir}")


def _exclusive_write(destination: Path, content: bytes, label: str) -> bool:
    _reject_link_or_reparse_point(destination, label)
    if destination.exists():
        if not destination.is_file():
            raise OSError(f"{label}不是常规文件：{destination}")
        return False
    try:
        destination_file = destination.open("xb")
    except FileExistsError:
        _reject_link_or_reparse_point(destination, label)
        if not destination.is_file():
            raise OSError(f"并发创建的{label}不是常规文件：{destination}")
        return False
    try:
        with destination_file:
            shutil.copyfileobj(io.BytesIO(content), destination_file)
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    return True


def _write_managed_files(
    management_dir: Path,
    templates: list[tuple[str, bytes]],
    *,
    dry_run: bool,
    include_config: bool,
) -> dict[str, Any]:
    planned = ([CONFIG_NAME] if include_config else []) + [
        filename for filename, _ in templates if not (management_dir / filename).exists()
    ]
    if dry_run:
        return {"mode": "managed", "dry_run": True, "created": planned, "skipped": []}

    _ensure_management_dir(management_dir)
    created: list[str] = []
    skipped: list[str] = []
    if include_config:
        if _exclusive_write(management_dir / CONFIG_NAME, _config_payload("managed"), "配置文件"):
            created.append(CONFIG_NAME)
        else:
            skipped.append(CONFIG_NAME)
    for filename, template_content in templates:
        if _exclusive_write(management_dir / filename, template_content, "目标文件"):
            created.append(filename)
        else:
            skipped.append(filename)
    return {"mode": "managed", "dry_run": False, "created": created, "skipped": skipped}


def initialize(project_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    if dry_run:
        inspection = inspect_project(root)
        return _initialize_from_inspection(root, inspection, dry_run=True)
    with _initialization_lock(root):
        inspection = inspect_project(root)
        return _initialize_from_inspection(root, inspection, dry_run=False)


def _initialize_from_inspection(
    root: Path, inspection: dict[str, Any], *, dry_run: bool
) -> dict[str, Any]:
    if inspection["mode"] == "external-ledger":
        raise ValueError("项目已采用现有账本，拒绝创建第二套管理文档")
    if inspection["mode"] == "legacy-managed":
        raise ValueError("检测到旧版 .project-to-act，请先使用 --migrate")
    if inspection["mode"] == "invalid-empty":
        raise ValueError("检测到空的 .project-to-act，请人工确认后修复或移除")
    external_ledgers = inspection.get("external_ledgers", [])
    if inspection["configured"] and inspection["mode"] == "managed" and external_ledgers:
        joined = "、".join(external_ledgers)
        raise ValueError(f"managed 模式同时存在外部账本 {joined}；请先确认唯一事实源")
    if not inspection["configured"] and external_ledgers:
        joined = "、".join(external_ledgers)
        raise ValueError(f"检测到现有项目账本 {joined}；请使用 --adopt-ledger，拒绝创建第二事实源")
    templates = _template_payloads()
    management_dir = _management_dir(root)
    include_config = not (management_dir / CONFIG_NAME).exists()
    return _write_managed_files(
        management_dir, templates, dry_run=dry_run, include_config=include_config
    )


def adopt_ledger(project_root: Path, ledger: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    _, relative_ledger = _safe_relative_file(root, ledger, "规范项目账本")
    inspection = inspect_project(root)
    if inspection["configured"]:
        if (
            inspection["mode"] == "external-ledger"
            and inspection.get("canonical_ledger") == relative_ledger
        ):
            return {
                "mode": "external-ledger",
                "dry_run": dry_run,
                "canonical_ledger": relative_ledger,
                "created": [],
                "skipped": [CONFIG_NAME],
            }
        raise ValueError("项目已有不同的 project-to-act 配置，拒绝改写")
    if inspection["mode"] != "unconfigured":
        raise ValueError("已有 .project-to-act 内容；请先验证或迁移，拒绝覆盖")
    detected_ledgers = inspection["external_ledgers"]
    if len(detected_ledgers) > 1 or (
        detected_ledgers and relative_ledger not in detected_ledgers
    ):
        raise ValueError("检测到多个或不一致的现有账本；请先人工确认唯一事实源")

    result = {
        "mode": "external-ledger",
        "dry_run": dry_run,
        "canonical_ledger": relative_ledger,
        "created": [CONFIG_NAME],
        "skipped": [],
    }
    if dry_run:
        return result
    management_dir = _management_dir(root)
    _ensure_management_dir(management_dir)
    if not _exclusive_write(
        management_dir / CONFIG_NAME,
        _config_payload("external-ledger", relative_ledger),
        "配置文件",
    ):
        raise FileExistsError(f"配置文件被并发创建：{management_dir / CONFIG_NAME}")
    return result


def migrate_legacy(project_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    inspection = inspect_project(root)
    if inspection["configured"]:
        return {
            "mode": inspection["mode"],
            "dry_run": dry_run,
            "created": [],
            "skipped": [CONFIG_NAME],
        }
    if inspection["mode"] != "legacy-managed":
        raise ValueError("未检测到可迁移的旧版 .project-to-act")
    if inspection["external_ledgers"]:
        raise ValueError("旧版管理目录与外部账本并存；请先人工确认唯一事实源")
    templates = _template_payloads()
    return _write_managed_files(
        _management_dir(root), templates, dry_run=dry_run, include_config=True
    )


def validate_project(project_root: Path) -> dict[str, Any]:
    root = _resolve_project_root(project_root)
    issues: list[str] = []
    try:
        inspection = inspect_project(root)
    except (ValueError, OSError) as error:
        return {"valid": False, "issues": [str(error)]}
    if not inspection["configured"]:
        issues.append(f"项目管理尚未配置；建议操作：{inspection['action']}")
        return {"valid": False, "mode": inspection["mode"], "issues": issues}

    mode = inspection["mode"]
    if mode == "managed":
        if inspection["external_ledgers"]:
            issues.append(
                "managed 模式存在外部账本，形成多个事实源："
                + "、".join(inspection["external_ledgers"])
            )
        management_dir = _management_dir(root)
        for filename in TEMPLATE_NAMES:
            path = management_dir / filename
            if not path.is_file():
                issues.append(f"缺少管理文件：{filename}")
                continue
            _reject_link_or_reparse_point(path, "管理文件")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                issues.append(f"管理文件不是有效 UTF-8：{filename}")
                continue
            for heading in REQUIRED_HEADINGS[filename]:
                if heading not in text:
                    issues.append(f"{filename} 缺少标题：{heading}")
    else:
        noncanonical = [
            path
            for path in inspection["external_ledgers"]
            if path != inspection["canonical_ledger"]
        ]
        if noncanonical:
            issues.append("存在非规范账本：" + "、".join(noncanonical))
        ledger_path = root / inspection["canonical_ledger"]
        try:
            text = ledger_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append("规范项目账本不是有效 UTF-8")
        else:
            heading_count = sum(1 for line in text.splitlines() if line.startswith("## "))
            if heading_count < 2:
                issues.append("规范项目账本至少需要两个二级标题以支持状态检索")
    return {
        "valid": not issues,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="安全发现、初始化、采用和验证项目管理文档。")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="明确的项目根目录；默认使用当前工作目录。",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="只检查现有管理模式，不写文件。")
    actions.add_argument("--validate", action="store_true", help="验证当前配置和文档结构。")
    actions.add_argument("--migrate", action="store_true", help="为旧版五文档配置补充 schema 配置。")
    actions.add_argument("--adopt-ledger", metavar="PATH", help="采用项目内已有账本，不复制其内容。")
    parser.add_argument("--dry-run", action="store_true", help="预览初始化、采用或迁移，不写文件。")
    args = parser.parse_args()
    try:
        if args.check:
            result = inspect_project(args.project_root)
        elif args.validate:
            result = validate_project(args.project_root)
        elif args.migrate:
            result = migrate_legacy(args.project_root, dry_run=args.dry_run)
        elif args.adopt_ledger:
            result = adopt_ledger(
                args.project_root, args.adopt_ledger, dry_run=args.dry_run
            )
        else:
            result = initialize(args.project_root, dry_run=args.dry_run)
    except (ValueError, FileNotFoundError, OSError) as error:
        parser.exit(1, f"操作失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False))
    if args.validate and not result["valid"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
