import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "project-to-act" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from hook_adapter import handle_event
from codex_hook_adapter import adapt_codex_event
from install_collaboration_runtime import CODEX_HOOK_COMMAND, doctor_runtime, install_runtime, uninstall_runtime
from runtime_transaction import FileTransaction
import task_runtime as runtime
from task_runtime import RuntimeFailure, discover_task
from tests.test_task_runtime import git, make_repository, write_json


def managed_repository(root: Path) -> None:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    write_json(root / ".project-to-act/PROJECT_CONFIG.json", {"schema_version": 1, "mode": "managed"})
    git(root, "add", ".")
    git(root, "commit", "-m", "test: initialize managed project")


class RuntimeAdapterInstallTests(unittest.TestCase):
    def test_dry_run_and_install_are_non_overwriting_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            preview = install_runtime(root, dry_run=True)
            self.assertTrue(preview["dryRun"])
            self.assertFalse((root / ".project-to-act/bin").exists())
            self.assertFalse((root / "AGENTS.md").exists())

            installed = install_runtime(root)
            self.assertFalse(installed["gitHookActive"])
            self.assertEqual(installed["agentsAction"], "created")
            config = json.loads((root / ".project-to-act/COLLABORATION_CONFIG.json").read_text(encoding="utf-8"))
            self.assertEqual(config, {"schemaVersion": 1, "experimentalHandoffWrites": False})
            for filename in ("task_view.py", "runtime_transaction.py", "task_runtime.py", "hook_adapter.py"):
                self.assertTrue((root / ".project-to-act/bin" / filename).is_file())
            self.assertTrue((root / ".github/workflows/project-to-act.yml").is_file())
            if os.name != "nt":
                self.assertTrue((root / ".project-to-act/hooks/pre-commit").stat().st_mode & stat.S_IXUSR)

            second = install_runtime(root)
            self.assertEqual(second["createdOrUpdated"], [])
            self.assertEqual(second["agentsAction"], "unchanged")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            agents_path = root / "AGENTS.md"
            agents_path.write_text("# Existing repository guidance\n", encoding="utf-8")
            installed = install_runtime(root)
            self.assertEqual(installed["agentsAction"], "appended")
            self.assertTrue(agents_path.read_text(encoding="utf-8").startswith("# Existing repository guidance\n"))
            self.assertEqual(install_runtime(root)["agentsAction"], "unchanged")

    def test_installed_runtime_runs_without_global_skill_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            install_runtime(root)
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: install repository runtime")
            adapter = root / ".project-to-act/bin/hook_adapter.py"
            result = subprocess.run(
                [sys.executable, str(adapter), "--project-root", str(root), "--event", "session-start"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["taskId"], "TASK-001")

            task_runtime = root / ".project-to-act/bin/task_runtime.py"
            validated = subprocess.run(
                [sys.executable, str(task_runtime), "--project-root", str(root), "validate", "TASK-001"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])

            codex_adapter = root / ".project-to-act/bin/codex_hook_adapter.py"
            codex = subprocess.run(
                [sys.executable, str(codex_adapter)],
                cwd=root,
                input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "s-1"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertEqual(codex.returncode, 0, codex.stderr)
            self.assertEqual(json.loads(codex.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(list((root / ".project-to-act/bin").rglob("__pycache__")), [])
            self.assertEqual(list((root / ".project-to-act/bin").rglob("*.pyc")), [])

    def test_codex_adapter_maps_official_lifecycle_payloads_without_semantic_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            start = adapt_codex_event({"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "s-1"})
            self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("TASK-001", start["hookSpecificOutput"]["additionalContext"])

            dirty = root / "src/other.txt"
            dirty.write_text("work in progress\n", encoding="utf-8")
            stop = adapt_codex_event({"hook_event_name": "Stop", "cwd": str(root), "stop_hook_active": False})
            self.assertTrue(stop["continue"])
            self.assertIn("SEMANTIC", stop["systemMessage"].upper())
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.json").exists())
            self.assertIsNone(adapt_codex_event({"hook_event_name": "SessionEnd", "cwd": str(root), "reason": "clear"}))

    def test_codex_hook_install_is_explicit_and_existing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            install_runtime(root)
            self.assertFalse((root / ".codex/hooks.json").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            installed = install_runtime(root, install_codex_hook=True)
            hooks_path = root / ".codex/hooks.json"
            self.assertEqual(installed["codexHookAction"], "created")
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(hooks["hooks"]["Stop"][0]["hooks"][0]["command"], CODEX_HOOK_COMMAND)
            self.assertEqual(doctor_runtime(root, check_codex_hook=True)["codexHookStatus"], "valid")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            hooks_path = root / ".codex/hooks.json"
            write_json(hooks_path, {"hooks": {"Stop": []}})
            with self.assertRaises(RuntimeFailure) as conflict:
                install_runtime(root, install_codex_hook=True)
            self.assertEqual(conflict.exception.code, "INSTALL_CONFLICT")
            self.assertFalse((root / ".project-to-act/bin").exists())

    def test_pre_commit_validates_staged_intent_and_stop_only_discloses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            inside = root / "src/other.txt"
            inside.write_text("inside intent\n", encoding="utf-8")
            git(root, "add", "src/other.txt")
            accepted = handle_event(root, "pre-commit")
            self.assertTrue(accepted["result"]["valid"])
            git(root, "reset")
            inside.unlink()

            outside = root / "docs/outside.txt"
            outside.parent.mkdir()
            outside.write_text("outside intent\n", encoding="utf-8")
            git(root, "add", "docs/outside.txt")
            with self.assertRaises(RuntimeFailure) as conflict:
                handle_event(root, "pre-commit")
            self.assertEqual(conflict.exception.code, "INTENT_CONFLICT")
            git(root, "reset")

            disclosure = handle_event(root, "stop")
            self.assertTrue(disclosure["noOp"])
            self.assertEqual(disclosure["code"], "SEMANTIC_ACTION_REQUIRED")
            self.assertIn("docs/outside.txt", disclosure["changedPaths"])
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.json").exists())

    def test_pre_commit_does_not_expand_a_file_glob_to_the_whole_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            intent_path = root / ".project-to-act/tasks/TASK-001/INTENT.json"
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            intent["paths"] = ["src/*.py"]
            write_json(intent_path, intent)
            git(root, "add", ".project-to-act/tasks/TASK-001/INTENT.json")

            outside_glob = root / "src/nested/other.py"
            outside_glob.parent.mkdir()
            outside_glob.write_text("changed outside the file glob\n", encoding="utf-8")
            git(root, "add", "src/nested/other.py")
            with self.assertRaises(RuntimeFailure) as conflict:
                handle_event(root, "pre-commit")
            self.assertEqual(conflict.exception.code, "INTENT_CONFLICT")

    def test_pre_commit_bounds_the_staged_path_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            original_git_value = runtime._git_value

            def git_value(project_root, *args):
                if args == ("diff", "--cached", "--name-only", "--"):
                    return "\n".join(f"src/file-{index}.txt" for index in range(runtime.MAX_STAGED_PATHS + 1))
                return original_git_value(project_root, *args)

            with mock.patch.object(runtime, "_git_value", side_effect=git_value):
                with self.assertRaises(RuntimeFailure) as conflict:
                    runtime.validate_task(root, "TASK-001", staged=True)
            self.assertEqual(conflict.exception.code, "INTENT_CONFLICT")

    def test_task_discovery_zero_one_many_and_ci_detached_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            self.assertEqual(discover_task(root), "TASK-001")

            other = root / ".project-to-act/tasks/TASK-002"
            write_json(
                other / "STATUS.json",
                {"schemaVersion": 1, "taskId": "TASK-002", "state": "review", "revision": 1, "branch": "task/task-001"},
            )
            with self.assertRaises(RuntimeFailure) as many:
                discover_task(root)
            self.assertEqual(many.exception.code, "CANONICAL_SOURCE_CONFLICT")
            (other / "STATUS.json").unlink()
            other.rmdir()

            git(root, "checkout", "--detach")
            ci_environment = {"GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": "", "CI_COMMIT_REF_NAME": ""}
            with mock.patch.dict(os.environ, ci_environment, clear=False):
                with self.assertRaises(RuntimeFailure) as zero:
                    discover_task(root)
                self.assertEqual(zero.exception.code, "BRANCH_MISMATCH")
            with mock.patch.dict(os.environ, {**ci_environment, "GITHUB_HEAD_REF": "task/task-001"}, clear=False):
                result = handle_event(root, "ci")
            self.assertTrue(result["result"]["valid"])

    def test_explicit_task_still_requires_its_canonical_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            git(root, "checkout", "-b", "unrelated")
            with self.assertRaises(RuntimeFailure) as mismatch:
                handle_event(root, "session-start", task_id="TASK-001")
            self.assertEqual(mismatch.exception.code, "BRANCH_MISMATCH")

    def test_conflict_stops_before_overwrite_and_hook_activation_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            workflow = root / ".github/workflows/project-to-act.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: local workflow\n", encoding="utf-8")
            with self.assertRaises(RuntimeFailure) as initial_conflict:
                install_runtime(root)
            self.assertEqual(initial_conflict.exception.code, "INSTALL_CONFLICT")
            self.assertFalse((root / ".project-to-act/bin").exists())
            self.assertFalse((root / ".project-to-act/COLLABORATION_CONFIG.json").exists())
            self.assertFalse((root / "AGENTS.md").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            install_runtime(root)
            runtime_path = root / ".project-to-act/bin/task_runtime.py"
            runtime_path.write_text("local customization\n", encoding="utf-8")
            before = (root / "AGENTS.md").read_bytes()
            with self.assertRaises(RuntimeFailure) as conflict:
                install_runtime(root)
            self.assertEqual(conflict.exception.code, "INSTALL_CONFLICT")
            self.assertEqual((root / "AGENTS.md").read_bytes(), before)
            self.assertEqual(runtime_path.read_text(encoding="utf-8"), "local customization\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            install_runtime(root)
            agents_path = root / "AGENTS.md"
            agents_path.write_text(agents_path.read_text(encoding="utf-8").replace("never guess", "always guess"), encoding="utf-8")
            with self.assertRaises(RuntimeFailure) as marker_conflict:
                install_runtime(root)
            self.assertEqual(marker_conflict.exception.code, "INSTALL_CONFLICT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            activated = install_runtime(root, activate_git_hook=True)
            self.assertTrue(activated["gitHookActive"])
            self.assertEqual(git(root, "config", "--get", "core.hooksPath"), ".project-to-act/hooks")

    def test_doctor_and_controlled_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            agents_path = root / "AGENTS.md"
            agents_path.write_text("# Existing repository guidance\n", encoding="utf-8")
            install_runtime(root, activate_git_hook=True, install_codex_hook=True)
            diagnosis = doctor_runtime(root, check_codex_hook=True)
            self.assertTrue(diagnosis["valid"])
            self.assertTrue(diagnosis["gitHookActive"])
            self.assertEqual(diagnosis["codexHookStatus"], "valid")

            config_path = root / ".project-to-act/COLLABORATION_CONFIG.json"
            write_json(config_path, {"schemaVersion": 1, "experimentalHandoffWrites": True})
            preview = uninstall_runtime(root, dry_run=True)
            self.assertIn(".project-to-act/COLLABORATION_CONFIG.json", preview["preserved"])
            self.assertTrue((root / ".project-to-act/bin/task_runtime.py").exists())
            self.assertEqual(git(root, "config", "--get", "core.hooksPath"), ".project-to-act/hooks")

            removed = uninstall_runtime(root)
            self.assertTrue(removed["gitHookDeactivated"])
            self.assertFalse((root / ".project-to-act/bin").exists())
            self.assertFalse((root / ".project-to-act/hooks").exists())
            self.assertFalse((root / ".github/workflows/project-to-act.yml").exists())
            self.assertFalse((root / ".codex/hooks.json").exists())
            self.assertTrue(config_path.exists())
            self.assertEqual(agents_path.read_text(encoding="utf-8"), "# Existing repository guidance")
            hook_config = subprocess.run(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertNotEqual(hook_config.returncode, 0)

    def test_uninstall_fails_before_deleting_modified_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_repository(root)
            install_runtime(root)
            adapter = root / ".project-to-act/bin/hook_adapter.py"
            workflow = root / ".github/workflows/project-to-act.yml"
            adapter.write_text("local customization\n", encoding="utf-8")
            with self.assertRaises(RuntimeFailure) as conflict:
                uninstall_runtime(root)
            self.assertEqual(conflict.exception.code, "INSTALL_CONFLICT")
            self.assertTrue(workflow.exists())
            self.assertEqual(adapter.read_text(encoding="utf-8"), "local customization\n")

    def test_delete_transaction_rolls_back_removed_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first\n", encoding="utf-8")
            second.write_text("second\n", encoding="utf-8")
            transaction = FileTransaction(root, fail_after=1)
            transaction.add_delete(first)
            transaction.add_delete(second)
            with self.assertRaises(RuntimeFailure) as failure:
                transaction.commit()
            self.assertEqual(failure.exception.code, "PARTIAL_WRITE")
            self.assertEqual(first.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "second\n")


if __name__ == "__main__":
    unittest.main()
