import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "project-to-act" / "scripts"
SCRIPT_PATH = SCRIPT_DIR / "task_runtime.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    spec = importlib.util.spec_from_file_location("task_runtime", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load task_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = load_module()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def payload() -> dict[str, object]:
    return {
        "implementationSummary": "Implemented the canonical task view",
        "changedComponents": ["src/value.txt"],
        "changeRefs": ["HEAD"],
        "designDecisions": ["Keep one canonical writer"],
        "deviationsFromContract": [],
        "selfTestEvidenceRefs": [".project-to-act/tasks/TASK-001/evidence/E-001.json"],
        "setupOrMigrationSteps": [],
        "knownLimitations": [],
        "suspectedRiskAreas": ["handoff recovery"],
        "requestedTestFocus": ["independent verification"],
    }


def make_repository(root: Path, *, writes_enabled: bool = True) -> None:
    git(root, "init", "-b", "task/task-001")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    source = root / "src" / "value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("stable source\n", encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    task_dir = root / ".project-to-act" / "tasks" / "TASK-001"
    write_json(
        root / ".project-to-act" / "PROJECT_CONFIG.json",
        {"schema_version": 1, "mode": "managed"},
    )
    write_json(
        root / ".project-to-act" / "COLLABORATION_CONFIG.json",
        {"schemaVersion": 1, "experimentalHandoffWrites": writes_enabled},
    )
    write_json(
        task_dir / "TASK.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "title": "Implement one observable outcome",
            "owner": "alice",
            "goal": "Produce a verified handoff",
            "scope": {"allowed": ["src/**"], "nonGoals": ["Unrelated work"]},
            "acceptance": [{"text": "The handoff can be resumed", "status": "satisfied"}],
            "verification": [{"text": "python -m unittest", "status": "passed", "independence": "self"}],
        },
    )
    write_json(
        task_dir / "STATUS.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "state": "review",
            "revision": 3,
            "baseRevision": 0,
            "owner": "alice",
            "currentActor": "alice",
            "currentRole": "builder",
            "activeSessionId": None,
            "branch": "task/task-001",
            "contextHash": "ctx-001",
            "handoffState": None,
            "handoffId": None,
            "nextAction": "Publish verification candidate",
        },
    )
    write_json(
        task_dir / "CONTEXT.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "contextHash": "ctx-001",
            "inputs": [{"path": "src/value.txt", "sha256": source_digest}],
        },
    )
    write_json(
        task_dir / "INTENT.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "paths": ["src/**"],
            "symbols": ["canonicalTaskView"],
            "contractsWrite": ["project-to-act-handoff@1"],
            "migrations": False,
        },
    )
    write_json(
        task_dir / "evidence" / "E-001.json",
        {"schemaVersion": 1, "evidenceId": "E-001", "taskId": "TASK-001", "kind": "self-check", "status": "passed"},
    )
    (task_dir / "events").mkdir(parents=True)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: establish task fixture")


def publish(root: Path, **overrides):
    arguments = {
        "expected_revision": 3,
        "from_actor": "alice",
        "to_actor": "bob",
        "handoff_type": "builder_to_verifier.verification_candidate",
        "payload": payload(),
        "summary": "Canonical task view is ready for independent verification",
        "next_action": "Run independent verification",
    }
    arguments.update(overrides)
    return RUNTIME.publish_handoff(root, "TASK-001", **arguments)


class TaskRuntimeTests(unittest.TestCase):
    def test_runtime_input_boundaries(self):
        self.assertEqual(RUNTIME._identity("a" * 128, "actor"), "a" * 128)
        with self.assertRaises(RUNTIME.RuntimeFailure):
            RUNTIME._identity("a" * 129, "actor")
        with self.assertRaises(RUNTIME.RuntimeFailure):
            RUNTIME._identity("alice\tadmin", "actor")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            published = publish(root, summary="s" * 4000)
            self.assertEqual(len(published["handoff"]["summary"]), 4000)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            with self.assertRaises(RUNTIME.RuntimeFailure) as too_long:
                publish(root, summary="s" * 4001)
            self.assertEqual(too_long.exception.code, "HANDOFF_INCOMPLETE")

        with tempfile.TemporaryDirectory() as temp_dir:
            payload_path = Path(temp_dir) / "payload.json"
            payload_path.write_bytes(b"{" + b" " * RUNTIME.MAX_JSON_BYTES + b"}")
            with self.assertRaises(RUNTIME.RuntimeFailure) as too_large:
                RUNTIME._payload(str(payload_path))
            self.assertEqual(too_large.exception.code, "ROLE_PAYLOAD_INVALID")

    def test_cli_publish_accept_and_resume_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            root = sandbox / "repo"
            root.mkdir()
            make_repository(root)
            payload_path = sandbox / "payload.json"
            write_json(payload_path, payload())
            published = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--project-root",
                    str(root),
                    "handoff",
                    "publish",
                    "TASK-001",
                    "--expected-revision",
                    "3",
                    "--from",
                    "alice",
                    "--to",
                    "bob",
                    "--handoff-type",
                    "builder_to_verifier.verification_candidate",
                    "--payload-file",
                    str(payload_path),
                    "--summary",
                    "Canonical task view is ready for independent verification",
                    "--next-action",
                    "Run independent verification",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            self.assertEqual(json.loads(published.stdout)["handoff"]["state"], "published")
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")

            accepted = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--project-root",
                    str(root),
                    "handoff",
                    "accept",
                    "TASK-001",
                    "--expected-revision",
                    "4",
                    "--actor",
                    "bob",
                    "--executor",
                    "human",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout)["handoff"]["state"], "accepted")

            resumed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--project-root", str(root), "resume", "TASK-001"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertTrue(json.loads(resumed.stdout)["recoverable"])

    def test_publish_accept_and_cold_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            published = publish(root)
            self.assertFalse(published["noOp"])
            self.assertTrue(published["authorizationValidated"])
            self.assertEqual(published["status"]["revision"], 4)
            self.assertEqual(published["handoff"]["producerRole"], "builder")
            self.assertEqual(published["handoff"]["consumerRole"], "verifier")
            self.assertEqual(len(list((root / ".project-to-act/tasks/TASK-001/events").glob("*.json"))), 1)

            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            accepted = RUNTIME.accept_handoff(
                root,
                "TASK-001",
                expected_revision=4,
                actor="bob",
                executor="human",
            )
            self.assertFalse(accepted["noOp"])
            self.assertTrue(accepted["authorizationValidated"])
            self.assertEqual(accepted["status"]["revision"], 5)
            self.assertEqual(accepted["handoff"]["state"], "accepted")
            self.assertEqual(accepted["session"]["state"], "active")

            resumed = RUNTIME.resume_task(root, "TASK-001")
            self.assertTrue(resumed["recoverable"], resumed["errors"])
            self.assertEqual(resumed["session"]["actorId"], "bob")
            self.assertEqual(resumed["session"]["role"], "verifier")
            self.assertEqual(resumed["nextAction"], "Run independent verification")

    def test_duplicate_publish_and_accept_are_no_ops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            publish(root)
            event_dir = root / ".project-to-act/tasks/TASK-001/events"
            event_count = len(list(event_dir.glob("*.json")))
            duplicate = publish(root)
            self.assertTrue(duplicate["noOp"])
            self.assertEqual(len(list(event_dir.glob("*.json"))), event_count)

            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            accepted = RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="human")
            duplicate_accept = RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="human")
            self.assertTrue(duplicate_accept["noOp"])
            self.assertEqual(duplicate_accept["session"]["sessionId"], accepted["session"]["sessionId"])
            with self.assertRaises(RUNTIME.RuntimeFailure) as wrong_executor:
                RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="different-tool")
            self.assertEqual(wrong_executor.exception.code, "HANDOFF_ANCHOR_MISMATCH")
            with self.assertRaises(RUNTIME.RuntimeFailure) as wrong_revision:
                RUNTIME.accept_handoff(root, "TASK-001", expected_revision=999, actor="bob", executor="human")
            self.assertEqual(wrong_revision.exception.code, "STALE_TASK_REVISION")

    def test_stale_revision_and_dirty_tree_fail_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            with self.assertRaisesRegex(RUNTIME.RuntimeFailure, "Expected revision") as stale:
                publish(root, expected_revision=2)
            self.assertEqual(stale.exception.code, "STALE_TASK_REVISION")
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.json").exists())

            with self.assertRaises(RUNTIME.RuntimeFailure) as identity:
                publish(root, from_actor="alice\ninjected")
            self.assertEqual(identity.exception.code, "HANDOFF_INCOMPLETE")

            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RUNTIME.RuntimeFailure, "Uncommitted files") as dirty:
                publish(root)
            self.assertEqual(dirty.exception.code, "DIRTY_CHECKPOINT")
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.json").exists())

    def test_stale_context_and_failed_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            source = root / "src/value.txt"
            source.write_text("changed source\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "test: change source without rebuilding context")
            with self.assertRaisesRegex(RUNTIME.RuntimeFailure, "Context inputs changed") as stale:
                publish(root)
            self.assertEqual(stale.exception.code, "STALE_CONTEXT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            evidence_path = root / ".project-to-act/tasks/TASK-001/evidence/E-001.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["status"] = "failed"
            write_json(evidence_path, evidence)
            git(root, "add", ".")
            git(root, "commit", "-m", "test: record failed evidence")
            with self.assertRaisesRegex(RUNTIME.RuntimeFailure, "Evidence is not passed") as failed:
                publish(root)
            self.assertEqual(failed.exception.code, "HANDOFF_INCOMPLETE")

    def test_intent_conflict_blocks_publish_and_accept(self):
        self.assertTrue(RUNTIME._paths_overlap(".github/**", ".github/workflows/**"))
        self.assertFalse(RUNTIME._paths_overlap(".github/**", "github/**"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            other = root / ".project-to-act/tasks/TASK-002"
            write_json(other / "STATUS.json", {"schemaVersion": 1, "taskId": "TASK-002", "state": "in_progress", "revision": 1})
            write_json(
                other / "INTENT.json",
                {"schemaVersion": 1, "taskId": "TASK-002", "paths": ["src/value.txt"], "symbols": [], "contractsWrite": [], "migrations": False},
            )
            git(root, "add", ".")
            git(root, "commit", "-m", "test: add conflicting task intent")
            with self.assertRaises(RUNTIME.RuntimeFailure) as conflict:
                publish(root)
            self.assertEqual(conflict.exception.code, "INTENT_CONFLICT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            intent_path = root / ".project-to-act/tasks/TASK-001/INTENT.json"
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            intent["paths"] = ["../outside/**"]
            write_json(intent_path, intent)
            git(root, "add", ".")
            git(root, "commit", "-m", "test: add unsafe intent path")
            with self.assertRaises(RUNTIME.RuntimeFailure) as unsafe:
                publish(root)
            self.assertEqual(unsafe.exception.code, "INTENT_CONFLICT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            publish(root)
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            other = root / ".project-to-act/tasks/TASK-002"
            write_json(other / "STATUS.json", {"schemaVersion": 1, "taskId": "TASK-002", "state": "in_progress", "revision": 1})
            write_json(
                other / "INTENT.json",
                {"schemaVersion": 1, "taskId": "TASK-002", "paths": ["src/value.txt"], "symbols": [], "contractsWrite": [], "migrations": False},
            )
            git(root, "add", ".")
            git(root, "commit", "-m", "test: introduce conflict before accept")
            with self.assertRaises(RUNTIME.RuntimeFailure) as conflict:
                RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="human")
            self.assertEqual(conflict.exception.code, "INTENT_CONFLICT")

    def test_partial_write_rolls_back_every_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            status_path = root / ".project-to-act/tasks/TASK-001/STATUS.json"
            original_status = status_path.read_bytes()
            with self.assertRaisesRegex(RUNTIME.RuntimeFailure, "rolled back") as partial:
                publish(root, fail_after=2)
            self.assertEqual(partial.exception.code, "PARTIAL_WRITE")
            self.assertEqual(status_path.read_bytes(), original_status)
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.json").exists())
            self.assertFalse((root / ".project-to-act/tasks/TASK-001/HANDOFF.md").exists())
            self.assertEqual(list((root / ".project-to-act/tasks/TASK-001/events").glob("*.json")), [])

    def test_write_gate_wrong_target_and_anchor_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root, writes_enabled=False)
            with self.assertRaises(RUNTIME.RuntimeFailure) as disabled:
                publish(root)
            self.assertEqual(disabled.exception.code, "WRITE_PATH_DISABLED")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            publish(root)
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            with self.assertRaises(RUNTIME.RuntimeFailure) as target:
                RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="charlie", executor="human")
            self.assertEqual(target.exception.code, "ACTIVE_WRITER_CONFLICT")

            handoff_path = root / ".project-to-act/tasks/TASK-001/HANDOFF.json"
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            handoff["codeSha"] = "0" * 40
            write_json(handoff_path, handoff)
            git(root, "add", ".")
            git(root, "commit", "-m", "test: corrupt code anchor")
            with self.assertRaises(RUNTIME.RuntimeFailure) as anchor:
                RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="human")
            self.assertEqual(anchor.exception.code, "HANDOFF_ANCHOR_MISMATCH")

            resumed = RUNTIME.resume_task(root, "TASK-001")
            self.assertFalse(resumed["recoverable"])
            self.assertTrue(any(error["code"] == "HANDOFF_ANCHOR_MISMATCH" for error in resumed["errors"]))

    def test_active_lock_blocks_a_second_writer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            lock = root / ".project-to-act/runtime/locks/TASK-001.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text("pid=999999\n", encoding="utf-8")
            with self.assertRaises(RUNTIME.RuntimeFailure) as conflict:
                publish(root)
            self.assertEqual(conflict.exception.code, "ACTIVE_WRITER_CONFLICT")

    def test_wrong_branch_current_actor_and_accept_partial_write_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            git(root, "switch", "-c", "wrong-branch")
            with self.assertRaises(RUNTIME.RuntimeFailure) as branch:
                publish(root)
            self.assertEqual(branch.exception.code, "BRANCH_MISMATCH")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            status_path = root / ".project-to-act/tasks/TASK-001/STATUS.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["currentActor"] = "charlie"
            write_json(status_path, status)
            git(root, "add", ".")
            git(root, "commit", "-m", "test: change active writer")
            with self.assertRaises(RUNTIME.RuntimeFailure) as actor:
                publish(root)
            self.assertEqual(actor.exception.code, "ACTIVE_WRITER_CONFLICT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            status_path = root / ".project-to-act/tasks/TASK-001/STATUS.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["currentRole"] = "lead"
            write_json(status_path, status)
            git(root, "add", ".")
            git(root, "commit", "-m", "test: assign wrong producer role")
            with self.assertRaises(RUNTIME.RuntimeFailure) as role:
                publish(root)
            self.assertEqual(role.exception.code, "ROLE_DIRECTION_CONFLICT")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            publish(root)
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            handoff_path = root / ".project-to-act/tasks/TASK-001/HANDOFF.json"
            status_path = root / ".project-to-act/tasks/TASK-001/STATUS.json"
            original_handoff = handoff_path.read_bytes()
            original_status = status_path.read_bytes()
            with self.assertRaises(RUNTIME.RuntimeFailure) as partial:
                RUNTIME.accept_handoff(
                    root,
                    "TASK-001",
                    expected_revision=4,
                    actor="bob",
                    executor="human",
                    fail_after=3,
                )
            self.assertEqual(partial.exception.code, "PARTIAL_WRITE")
            self.assertEqual(handoff_path.read_bytes(), original_handoff)
            self.assertEqual(status_path.read_bytes(), original_status)
            self.assertEqual(list((root / ".project-to-act/runtime/sessions").glob("*.json")), [])

    def test_resume_reports_missing_accepted_session_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_repository(root)
            publish(root)
            git(root, "add", ".")
            git(root, "commit", "-m", "chore: publish handoff")
            accepted = RUNTIME.accept_handoff(root, "TASK-001", expected_revision=4, actor="bob", executor="human")
            session_path = root / ".project-to-act/runtime/sessions" / f"{accepted['session']['sessionId']}.json"
            session_path.unlink()
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
            resumed = RUNTIME.resume_task(root, "TASK-001")
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
            self.assertFalse(resumed["recoverable"])
            self.assertTrue(any(error["code"] == "HANDOFF_INCOMPLETE" for error in resumed["errors"]))
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
