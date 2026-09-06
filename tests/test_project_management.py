from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_project_management.py"
SPEC = importlib.util.spec_from_file_location("project_to_act", SCRIPT)
assert SPEC and SPEC.loader
pta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pta)


class ProjectToActTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize(self) -> Path:
        result = pta.initialize(self.root)
        self.assertEqual(result["schema_version"], 2)
        return self.root / ".project-to-act"

    def test_initialize_v2_and_validate(self) -> None:
        preview = pta.initialize(self.root, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertFalse((self.root / ".project-to-act").exists())
        management = self.initialize()
        config = json.loads((management / "PROJECT_CONFIG.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schema_version"], 2)
        self.assertEqual(config["policy"]["history_keep"], 10)
        self.assertEqual(config["feature_reconcile"]["expected_sources"], [])
        audit = pta.audit_project(self.root)
        self.assertTrue(audit["valid"])
        self.assertFalse(audit["errors"])

    def test_v1_is_compatible_and_migrates_without_content_change(self) -> None:
        management = self.initialize()
        config_path = management / "PROJECT_CONFIG.json"
        config_path.write_text('{"schema_version": 1, "mode": "managed"}\n', encoding="utf-8")
        before = {name: (management / name).read_bytes() for name in pta.TEMPLATE_NAMES}
        audit = pta.audit_project(self.root)
        self.assertTrue(audit["valid"])
        self.assertIn("SCHEMA_V1", {item["code"] for item in audit["warnings"]})
        preview = pta.migrate_project(self.root, dry_run=True)
        self.assertEqual(preview["schema_from"], 1)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["schema_version"], 1)
        pta.migrate_project(self.root)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["schema_version"], 2)
        self.assertEqual(before, {name: (management / name).read_bytes() for name in pta.TEMPLATE_NAMES})

    def test_legacy_directory_migration_adds_config_and_missing_templates(self) -> None:
        management = self.root / ".project-to-act"
        management.mkdir()
        (management / "PROJECT_OVERVIEW.md").write_text("# 项目总览\n", encoding="utf-8")
        preview = pta.migrate_project(self.root, dry_run=True)
        self.assertIn("PROJECT_CONFIG.json", preview["created"])
        self.assertFalse((management / "PROJECT_CONFIG.json").exists())
        result = pta.migrate_project(self.root)
        self.assertIn("PROJECT_CONFIG.json", result["created"])
        self.assertEqual((management / "PROJECT_OVERVIEW.md").read_text(encoding="utf-8"), "# 项目总览\n")
        self.assertTrue((management / "PROJECT_PROGRESS.md").is_file())

    def test_adopt_external_ledger_and_reject_compaction(self) -> None:
        ledger = self.root / "PROJECT_LEDGER.md"
        ledger.write_text("# Ledger\n\n## Goal\nX\n\n## Status\nY\n", encoding="utf-8")
        preview = pta.adopt_ledger(self.root, "PROJECT_LEDGER.md", dry_run=True)
        self.assertEqual(preview["canonical_ledger"], "PROJECT_LEDGER.md")
        self.assertFalse((self.root / ".project-to-act").exists())
        pta.adopt_ledger(self.root, "PROJECT_LEDGER.md")
        self.assertTrue(pta.audit_project(self.root)["valid"])
        with self.assertRaisesRegex(ValueError, "managed"):
            pta.compact_project(self.root, dry_run=True)

    def test_audit_detects_duplicate_heading_drift_and_size(self) -> None:
        management = self.initialize()
        progress = management / "PROJECT_PROGRESS.md"
        progress.write_text(progress.read_text(encoding="utf-8") + "\n## 当前任务\n", encoding="utf-8")
        features = management / "PROJECT_FEATURES.md"
        features.write_text(features.read_text(encoding="utf-8") + "\n## 公共 HTTP API 契约\n```json\n{}\n```\n", encoding="utf-8")
        acceptance = management / "PROJECT_ACCEPTANCE.md"
        acceptance.write_text(acceptance.read_text(encoding="utf-8") + ("x" * 340_000), encoding="utf-8")
        audit = pta.audit_project(self.root)
        error_codes = {item["code"] for item in audit["errors"]}
        warning_codes = {item["code"] for item in audit["warnings"]}
        self.assertIn("DUPLICATE_HEADING", error_codes)
        self.assertIn("UNEXPECTED_H2", warning_codes)
        self.assertIn("EMBEDDED_CODE_BLOCK", warning_codes)
        self.assertIn("SIZE_BUDGET_EXCEEDED", warning_codes)

    def test_strict_cli_fails_on_warning_but_normal_audit_does_not(self) -> None:
        management = self.initialize()
        features = management / "PROJECT_FEATURES.md"
        features.write_text(features.read_text(encoding="utf-8") + "\n## API 契约\n", encoding="utf-8")
        normal = subprocess.run(
            [sys.executable, str(SCRIPT), "--project-root", str(self.root), "--audit"],
            check=False,
            capture_output=True,
            text=True,
        )
        strict = subprocess.run(
            [sys.executable, str(SCRIPT), "--project-root", str(self.root), "--audit", "--strict"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(strict.returncode, 1)

    def test_duplicate_canonical_id_is_error(self) -> None:
        management = self.initialize()
        features = management / "PROJECT_FEATURES.md"
        text = features.read_text(encoding="utf-8")
        marker = "|---|---|---|---|---|---|---|---|---|---|\n"
        rows = "| F-001 | One | spec.md | P1 | 进行中 | 未检查 | 无 | done | docs/a.md | E-001 |\n| F-001 | Two | spec.md | P2 | 候选 | 未检查 | 无 | done | docs/b.md | E-002 |\n"
        features.write_text(text.replace(marker, marker + rows, 1), encoding="utf-8")
        audit = pta.audit_project(self.root)
        self.assertIn("DUPLICATE_CANONICAL_ID", {item["code"] for item in audit["errors"]})

    def test_extra_project_document_is_warning(self) -> None:
        management = self.initialize()
        (management / "PROJECT_KNOWLEDGE.md").write_text("# Knowledge\n", encoding="utf-8")
        audit = pta.audit_project(self.root)
        self.assertIn("EXTRA_PROJECT_DOCUMENT", {item["code"] for item in audit["warnings"]})

    def test_compaction_dry_run_archive_restore_and_idempotence(self) -> None:
        management = self.initialize()
        progress = management / "PROJECT_PROGRESS.md"
        text = progress.read_text(encoding="utf-8")
        history_table = (
            "| 日期 | 会话或任务 ID | 工作内容摘要 | 关键确认或纠正 | 证据 ID | 遗留问题 | 下一步 |\n"
            "|---|---|---|---|---|---|---|\n"
        )
        rows = "".join(
            f"| 2026-{month:02d}-01 | task-{index} | item-{index} | user | E-{index:03d} | none | next |\n"
            for index, month in enumerate([8, 8, 8, 7, 7, 6, 6, 5, 5, 4, 3, 2], start=1)
        )
        progress.write_text(text.replace(history_table, history_table + rows, 1), encoding="utf-8")
        preimage = progress.read_bytes()
        preview = pta.compact_project(self.root, dry_run=True)
        self.assertEqual(preview["archived_records"], 2)
        self.assertFalse((management / "archive").exists())
        result = pta.compact_project(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual(result["archived_records"], 2)
        active = progress.read_text(encoding="utf-8")
        self.assertNotIn("item-11", active)
        self.assertNotIn("item-12", active)
        self.assertIn("历史归档", active)
        self.assertTrue((management / "archive" / "progress" / "2026-03.md").is_file())
        self.assertTrue((management / "archive" / "progress" / "2026-02.md").is_file())
        manifest = management / result["manifest"]
        self.assertTrue(manifest.is_file())
        self.assertTrue(pta.audit_project(self.root)["valid"])
        again = pta.compact_project(self.root)
        self.assertFalse(again["changed"])
        restored = pta.restore_compaction(self.root, result["manifest"])
        self.assertEqual(restored["restored"], 1)
        self.assertEqual(progress.read_bytes(), preimage)
        self.assertTrue((management / "archive" / "progress" / "2026-03.md").is_file())

    def test_evidence_compaction_keeps_referenced_and_latest_unreferenced(self) -> None:
        management = self.initialize()
        acceptance = management / "PROJECT_ACCEPTANCE.md"
        text = acceptance.read_text(encoding="utf-8")
        evidence_table = (
            "| 证据 ID | 时间 | 方法摘要 | 退出状态 | 版本或文件哈希 | 结果摘要 | 证据位置 | 有效期 |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        rows = "".join(
            f"| E-{index:03d} | 2026-08-{(index % 27) + 1:02d} | test | 0 | hash | ok | reports/{index}.txt | 2026-09-30 |\n"
            for index in range(1, 24)
        )
        text = text.replace(evidence_table, evidence_table + rows, 1)
        text = text.replace("| A-001 | 项目目标达到可验证结果 | 待检查 | 对照项目目标 | 无 |", "| A-001 | 项目目标达到可验证结果 | 通过 | 对照项目目标 | E-023 |")
        acceptance.write_text(text, encoding="utf-8")
        preview = pta.compact_project(self.root, dry_run=True)
        acceptance_plan = next(item for item in preview["planned_documents"] if item["file"] == "PROJECT_ACCEPTANCE.md")
        self.assertEqual(acceptance_plan["archived_records"], 2)
        pta.compact_project(self.root)
        active = acceptance.read_text(encoding="utf-8")
        self.assertIn("E-023", active)
        self.assertNotIn("E-021 |", active)
        self.assertNotIn("E-022 |", active)

    def test_atomic_write_rejects_concurrent_change(self) -> None:
        path = self.root / "file.txt"
        path.write_text("before", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "并发冲突"):
            pta._atomic_write(path, b"after", expected_hash="0" * 64)
        self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def configure_reconcile(self, management: Path, expected: list[str], implemented: list[str]) -> None:
        config_path = management / "PROJECT_CONFIG.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["feature_reconcile"]["expected_sources"] = expected
        config["feature_reconcile"]["implemented_sources"] = implemented
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def add_feature_row(
        self,
        management: Path,
        row: str = "| F-001 | One | expected.md | P0 | 已完成 | 通过 | 无 | done | docs/a.md | E-001 |\n",
    ) -> None:
        features = management / "PROJECT_FEATURES.md"
        text = features.read_text(encoding="utf-8")
        marker = "|---|---|---|---|---|---|---|---|---|---|\n"
        features.write_text(text.replace(marker, marker + row, 1), encoding="utf-8")

    def test_reconcile_is_explicit_and_unconfigured_does_not_search(self) -> None:
        management = self.initialize()
        result = pta.reconcile_features(self.root)
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["source_files_read"], 0)
        self.assertFalse((management / "state").exists())

    def test_ordinary_audit_never_resolves_feature_sources(self) -> None:
        management = self.initialize()
        self.configure_reconcile(management, ["missing-expected.md"], ["missing-code.md"])
        self.assertEqual(pta.inspect_project(self.root)["mode"], "managed")
        self.assertTrue(pta.validate_project(self.root)["valid"])
        self.assertTrue(pta.audit_project(self.root)["valid"])
        self.assertFalse((management / "state").exists())
        with self.assertRaisesRegex(ValueError, "不存在"):
            pta.reconcile_features(self.root)

    def test_reconcile_rejects_globs_and_directories(self) -> None:
        management = self.initialize()
        self.configure_reconcile(management, ["*.md"], [])
        with self.assertRaisesRegex(ValueError, "无通配符"):
            pta.reconcile_features(self.root)
        self.configure_reconcile(management, ["assets"], [])
        with self.assertRaisesRegex(ValueError, "不是常规文件"):
            pta.reconcile_features(self.root)

    def test_reconcile_reports_id_differences_and_skips_unchanged_inputs(self) -> None:
        management = self.initialize()
        self.add_feature_row(management)
        (self.root / "expected.md").write_text("F-001 One\nF-002 Two\n", encoding="utf-8")
        (self.root / "implemented.md").write_text("F-001 One\nF-003 Three\n", encoding="utf-8")
        self.configure_reconcile(management, ["expected.md"], ["implemented.md"])
        first = pta.reconcile_features(self.root)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["source_files_read"], 2)
        self.assertEqual(first["differences"]["expected_unregistered"]["ids"], ["F-002"])
        self.assertEqual(first["differences"]["implemented_unregistered"]["ids"], ["F-003"])
        state = management / "state" / "feature_reconcile.json"
        self.assertTrue(state.is_file())
        before = state.read_bytes()
        second = pta.reconcile_features(self.root)
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "inputs_unchanged")
        self.assertEqual(second["source_files_read"], 0)
        self.assertEqual(state.read_bytes(), before)

    def test_reconcile_strict_cli_fails_on_cached_differences(self) -> None:
        management = self.initialize()
        self.add_feature_row(management)
        (self.root / "expected.md").write_text("F-001\nF-002\n", encoding="utf-8")
        self.configure_reconcile(management, ["expected.md"], [])
        pta.reconcile_features(self.root)
        strict = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--project-root",
                str(self.root),
                "--reconcile-features",
                "--strict",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(strict.returncode, 1)
        output = json.loads(strict.stdout)
        self.assertEqual(output["status"], "skipped")
        self.assertEqual(output["source_files_read"], 0)

    def test_reconcile_lock_blocks_duplicate_run(self) -> None:
        management = self.initialize()
        (self.root / "expected.md").write_text("F-001\n", encoding="utf-8")
        self.configure_reconcile(management, ["expected.md"], [])
        state_dir = management / "state"
        state_dir.mkdir()
        (state_dir / "feature_reconcile.lock").write_text("busy\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "已在运行"):
            pta.reconcile_features(self.root)

    def test_audit_warns_on_unverified_completed_feature(self) -> None:
        management = self.initialize()
        self.add_feature_row(
            management,
            "| F-001 | One | - | P0 | 已完成 | 未检查 | 无 | done | docs/a.md | 待记录 |\n",
        )
        codes = {item["code"] for item in pta.audit_project(self.root)["warnings"]}
        self.assertIn("FEATURE_SOURCE_MISSING", codes)
        self.assertIn("COMPLETED_FEATURE_WITHOUT_EVIDENCE", codes)
        self.assertIn("COMPLETED_FEATURE_NOT_ACCEPTED", codes)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unsupported")
    def test_symlink_management_path_is_rejected(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / ".project-to-act"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(OSError):
            pta.inspect_project(self.root)


if __name__ == "__main__":
    unittest.main()
