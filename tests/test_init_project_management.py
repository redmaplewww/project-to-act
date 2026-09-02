import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "project-to-act" / "scripts" / "init_project_management.py"
EXPECTED_FILES = (
    "PROJECT_OVERVIEW.md",
    "PROJECT_PROGRESS.md",
    "PROJECT_VERSIONS.md",
    "PROJECT_FEATURES.md",
    "PROJECT_ACCEPTANCE.md",
)
CONFIG_NAME = "PROJECT_CONFIG.json"
EXPECTED_CREATED = (CONFIG_NAME, *EXPECTED_FILES)


def load_module():
    spec = importlib.util.spec_from_file_location("init_project_management", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载初始化脚本：{SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    project_root: Path,
    script_path: Path = SCRIPT_PATH,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONUTF8", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--project-root",
            str(project_root),
            *extra_args,
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=environment,
    )


def make_isolated_script(root: Path, missing_template: str | None = None) -> Path:
    skill_root = root / "project-to-act"
    script_dir = skill_root / "scripts"
    template_dir = skill_root / "assets" / "templates"
    script_dir.mkdir(parents=True)
    template_dir.mkdir(parents=True)
    script_copy = script_dir / SCRIPT_PATH.name
    script_copy.write_bytes(SCRIPT_PATH.read_bytes())
    for filename in EXPECTED_FILES:
        if filename != missing_template:
            (template_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    return script_copy


class InitializeProjectManagementTests(unittest.TestCase):
    def test_creates_five_templates_in_empty_project(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = module.initialize(root)
            management_dir = root / ".project-to-act"

            self.assertEqual(
                result,
                {
                    "mode": "managed",
                    "dry_run": False,
                    "created": list(EXPECTED_CREATED),
                    "skipped": [],
                },
            )
            self.assertEqual({path.name for path in management_dir.iterdir()}, set(EXPECTED_CREATED))
            for filename in EXPECTED_FILES:
                self.assertTrue((management_dir / filename).read_text(encoding="utf-8").startswith("# "))

    def test_second_run_preserves_existing_content(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module.initialize(root)
            overview = root / ".project-to-act" / "PROJECT_OVERVIEW.md"
            overview.write_text("用户自定义内容\n", encoding="utf-8")

            result = module.initialize(root)

            self.assertEqual(
                result,
                {
                    "mode": "managed",
                    "dry_run": False,
                    "created": [],
                    "skipped": list(EXPECTED_FILES),
                },
            )
            self.assertEqual(overview.read_text(encoding="utf-8"), "用户自定义内容\n")

    def test_migrate_partial_directory_only_adds_missing_files_and_config(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            management_dir = root / ".project-to-act"
            management_dir.mkdir()
            existing = management_dir / "PROJECT_PROGRESS.md"
            existing.write_text("保留此内容\n", encoding="utf-8")

            result = module.migrate_legacy(root)

            self.assertEqual(
                result,
                {
                    "mode": "managed",
                    "dry_run": False,
                    "created": [
                        CONFIG_NAME,
                        *[name for name in EXPECTED_FILES if name != "PROJECT_PROGRESS.md"],
                    ],
                    "skipped": ["PROJECT_PROGRESS.md"],
                },
            )
            self.assertEqual(existing.read_text(encoding="utf-8"), "保留此内容\n")

    def test_default_init_refuses_existing_project_ledger(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "docs" / "project-ledger.md"
            ledger.parent.mkdir()
            ledger.write_text("# 项目账本\n\n## 目标\n\n## 状态\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "拒绝创建第二事实源"):
                module.initialize(root)

            self.assertFalse((root / ".project-to-act").exists())

    def test_adopt_existing_ledger_creates_config_only(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "docs" / "project-ledger.md"
            ledger.parent.mkdir()
            ledger.write_text("# 项目账本\n\n## 目标\n\n## 状态\n", encoding="utf-8")

            result = module.adopt_ledger(root, "docs/project-ledger.md")

            self.assertEqual(result["mode"], "external-ledger")
            self.assertEqual(result["canonical_ledger"], "docs/project-ledger.md")
            management_dir = root / ".project-to-act"
            self.assertEqual({path.name for path in management_dir.iterdir()}, {CONFIG_NAME})
            config = json.loads((management_dir / CONFIG_NAME).read_text(encoding="utf-8"))
            self.assertEqual(
                config,
                {
                    "schema_version": 1,
                    "mode": "external-ledger",
                    "canonical_ledger": "docs/project-ledger.md",
                },
            )

    def test_dry_run_adopt_writes_nothing(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "PROJECT_LEDGER.md"
            ledger.write_text("# Ledger\n\n## Goal\n\n## Status\n", encoding="utf-8")

            result = module.adopt_ledger(root, ledger, dry_run=True)

            self.assertTrue(result["dry_run"])
            self.assertFalse((root / ".project-to-act").exists())

    def test_adopt_rejects_ledger_outside_project(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as other_dir:
            root = Path(temp_dir)
            outside = Path(other_dir) / "PROJECT_LEDGER.md"
            outside.write_text("# Outside\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "项目根目录内"):
                module.adopt_ledger(root, outside)

    def test_validate_managed_project_and_detects_missing_heading(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module.initialize(root)

            valid_report = module.validate_project(root)
            self.assertTrue(valid_report["valid"], valid_report["issues"])

            overview = root / ".project-to-act" / "PROJECT_OVERVIEW.md"
            overview.write_text("# 项目总览\n", encoding="utf-8")
            invalid_report = module.validate_project(root)

            self.assertFalse(invalid_report["valid"])
            self.assertTrue(
                any("PROJECT_OVERVIEW.md 缺少标题" in issue for issue in invalid_report["issues"])
            )

    def test_initialize_dry_run_reports_all_files_without_writing(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            result = module.initialize(root, dry_run=True)

            self.assertEqual(result["created"], list(EXPECTED_CREATED))
            self.assertTrue(result["dry_run"])
            self.assertFalse((root / ".project-to-act").exists())

    def test_managed_project_ignores_explicit_legacy_ledger_redirect(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module.initialize(root)
            legacy = root / "docs" / "project-ledger.md"
            legacy.parent.mkdir()
            legacy.write_text(
                "<!-- project-to-act-redirect: .project-to-act/PROJECT_OVERVIEW.md -->\n"
                "# 项目账本已迁移\n",
                encoding="utf-8",
            )

            inspection = module.inspect_project(root)
            validation = module.validate_project(root)

            self.assertEqual(inspection["external_ledgers"], [])
            self.assertEqual(
                inspection["redirects"],
                {"docs/project-ledger.md": ".project-to-act/PROJECT_OVERVIEW.md"},
            )
            self.assertTrue(validation["valid"], validation["issues"])

    def test_refuses_multiple_existing_ledgers(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "PROJECT_LEDGER.md").write_text("# One\n", encoding="utf-8")
            second = root / "docs" / "project-ledger.md"
            second.parent.mkdir()
            second.write_text("# Two\n", encoding="utf-8")

            inspection = module.inspect_project(root)
            self.assertEqual(
                inspection["external_ledgers"],
                ["PROJECT_LEDGER.md", "docs/project-ledger.md"],
            )
            with self.assertRaisesRegex(ValueError, "多个或不一致"):
                module.adopt_ledger(root, "PROJECT_LEDGER.md")

    def test_validate_rejects_unknown_schema_version(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module.initialize(root)
            config = root / ".project-to-act" / CONFIG_NAME
            config.write_text(
                json.dumps({"schema_version": 999, "mode": "managed"}),
                encoding="utf-8",
            )

            report = module.validate_project(root)

            self.assertFalse(report["valid"])
            self.assertTrue(any("不支持的项目管理 schema" in issue for issue in report["issues"]))

    def test_inspection_rejects_config_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            management = root / ".project-to-act"
            management.mkdir()
            config = root.resolve() / ".project-to-act" / CONFIG_NAME
            original_is_symlink = Path.is_symlink

            def report_config_as_symlink(path):
                return path == config or original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", report_config_as_symlink):
                with self.assertRaisesRegex(OSError, "配置文件不得是符号链接"):
                    module.inspect_project(root)

    def test_inspection_rejects_broken_ledger_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root.resolve() / "docs" / "project-ledger.md"
            original_is_symlink = Path.is_symlink

            def report_ledger_as_symlink(path):
                return path == ledger or original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", report_ledger_as_symlink):
                with self.assertRaisesRegex(OSError, "现有项目账本不得是符号链接"):
                    module.inspect_project(root)

    def test_rejects_file_as_project_root(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_file = Path(temp_dir) / "not-a-directory"
            project_file.write_text("x", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "项目根路径不是目录"):
                module.initialize(project_file)

    def test_preflights_every_template_before_creating_management_directory(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_path = make_isolated_script(root, missing_template="PROJECT_ACCEPTANCE.md")
            module.__file__ = str(script_path)
            project_root = root / "project"
            project_root.mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "PROJECT_ACCEPTANCE.md"):
                module.initialize(project_root)

            self.assertFalse((project_root / ".project-to-act").exists())

    def test_preflights_template_readability_before_project_mutation(self):
        module = load_module()
        original_open = Path.open

        def reject_acceptance_template(path, mode="r", *args, **kwargs):
            if path.name == "PROJECT_ACCEPTANCE.md" and mode == "rb":
                raise PermissionError("injected unreadable template")
            return original_open(path, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            Path, "open", reject_acceptance_template
        ):
            root = Path(temp_dir)
            with self.assertRaisesRegex(PermissionError, "injected unreadable template"):
                module.initialize(root)

            self.assertFalse((root / ".project-to-act").exists())

    def test_rejects_management_path_that_is_a_regular_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".project-to-act").write_text("collision", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "管理路径"):
                module.initialize(root)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_rejects_management_directory_junction_without_writing_outside(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            outside = root / "outside"
            project_root.mkdir()
            outside.mkdir()
            junction = project_root / ".project-to-act"
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

            with self.assertRaisesRegex(OSError, "重解析点"):
                module.initialize(project_root)

            self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_directory_at_template_destination(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            collision = root / ".project-to-act" / "PROJECT_OVERVIEW.md"
            collision.mkdir(parents=True)

            with self.assertRaisesRegex(OSError, "PROJECT_OVERVIEW.md"):
                module.migrate_legacy(root)

    def test_rejects_symlink_at_template_destination(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            management_dir = root / ".project-to-act"
            management_dir.mkdir()
            collision = root.resolve() / ".project-to-act" / "PROJECT_OVERVIEW.md"
            original_is_symlink = Path.is_symlink

            def report_collision_as_symlink(path):
                return path == collision or original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", report_collision_as_symlink):
                with self.assertRaisesRegex(OSError, "符号链接"):
                    module.migrate_legacy(root)
            self.assertFalse(collision.exists())

    def test_exclusive_create_preserves_file_created_during_race(self):
        module = load_module()
        original_open = Path.open
        injected = False

        def racing_open(path, mode="r", *args, **kwargs):
            nonlocal injected
            if path.name == "PROJECT_OVERVIEW.md" and mode == "xb" and not injected:
                injected = True
                with original_open(path, "wb") as racing_file:
                    racing_file.write(b"racer-content")
            return original_open(path, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(Path, "open", racing_open):
            root = Path(temp_dir)
            result = module.initialize(root)
            overview = root / ".project-to-act" / "PROJECT_OVERVIEW.md"

            self.assertTrue(injected, "initializer must create destinations with Path.open('xb')")
            self.assertEqual(overview.read_bytes(), b"racer-content")
            self.assertEqual(result["skipped"], ["PROJECT_OVERVIEW.md"])

    def test_removes_new_destination_when_copying_fails(self):
        module = load_module()

        def fail_after_partial_copy(source, destination):
            destination.write(b"partial")
            raise OSError("injected copy failure")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            module.shutil, "copyfileobj", side_effect=fail_after_partial_copy
        ):
            root = Path(temp_dir)
            with self.assertRaisesRegex(OSError, "injected copy failure"):
                module.initialize(root)

            self.assertFalse((root / ".project-to-act" / "PROJECT_OVERVIEW.md").exists())


class InitializerCliTests(unittest.TestCase):
    def test_cli_success_emits_deterministic_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cli(Path(temp_dir))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "mode": "managed",
                "dry_run": False,
                "created": list(EXPECTED_CREATED),
                "skipped": [],
            },
        )

    def test_cli_error_is_nonzero_and_emits_no_success_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_file = Path(temp_dir) / "not-a-directory"
            project_file.write_text("x", encoding="utf-8")
            result = run_cli(project_file)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("操作失败", result.stderr)

    def test_cli_rejects_destination_collision_without_success_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".project-to-act" / "PROJECT_OVERVIEW.md").mkdir(parents=True)
            result = run_cli(root, SCRIPT_PATH, "--migrate")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("PROJECT_OVERVIEW.md", result.stderr)

    def test_cli_missing_template_fails_before_project_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_path = make_isolated_script(root, missing_template="PROJECT_ACCEPTANCE.md")
            project_root = root / "project"
            project_root.mkdir()
            result = run_cli(project_root, script_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("PROJECT_ACCEPTANCE.md", result.stderr)
            self.assertFalse((project_root / ".project-to-act").exists())

    def test_cli_check_reports_existing_ledger_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "docs" / "project-ledger.md"
            ledger.parent.mkdir()
            ledger.write_text("# 项目账本\n", encoding="utf-8")

            result = run_cli(root, SCRIPT_PATH, "--check")

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["action"], "adopt-ledger")
            self.assertEqual(report["external_ledgers"], ["docs/project-ledger.md"])
            self.assertFalse((root / ".project-to-act").exists())

    def test_cli_validate_external_ledger(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "docs" / "project-ledger.md"
            ledger.parent.mkdir()
            ledger.write_text("# 项目账本\n\n## 目标\n\n## 状态\n", encoding="utf-8")
            module.adopt_ledger(root, ledger)

            result = run_cli(root, SCRIPT_PATH, "--validate")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["valid"])

    def test_concurrent_cli_runs_create_each_file_exactly_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            environment = os.environ.copy()
            environment.pop("PYTHONUTF8", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            command = [sys.executable, str(SCRIPT_PATH), "--project-root", str(root)]
            processes = [
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                )
                for _ in range(8)
            ]
            results = [process.communicate(timeout=30) for process in processes]

            for process, (stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, "")
                self.assertEqual(
                    set(json.loads(stdout)), {"mode", "dry_run", "created", "skipped"}
                )
            reports = [json.loads(stdout) for stdout, _ in results]
            created = [filename for report in reports for filename in report["created"]]
            self.assertEqual(sorted(created), sorted(EXPECTED_CREATED))
            self.assertEqual(
                {path.name for path in (root / ".project-to-act").iterdir()}, set(EXPECTED_CREATED)
            )
            self.assertFalse((root / ".project-to-act.init.lock").exists())

    def test_stale_initialization_lock_fails_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock = root / module.INIT_LOCK_NAME
            lock.write_text("pid=999999\n", encoding="utf-8")
            with mock.patch.object(module, "INIT_LOCK_TIMEOUT_SECONDS", 0.0):
                with self.assertRaisesRegex(OSError, "等待并发初始化超时"):
                    module.initialize(root)
            self.assertTrue(lock.is_file())


if __name__ == "__main__":
    unittest.main()
