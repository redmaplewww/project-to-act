import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "project-to-act" / "scripts" / "task_view.py"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_task(root: Path, *, state: str = "in_progress") -> Path:
    task_dir = root / ".project-to-act" / "tasks" / "TASK-001"
    write_json(
        task_dir / "TASK.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "title": "Implement one observable outcome",
            "owner": "alice",
            "goal": "Return the same source-backed task semantics",
            "scope": {"allowed": ["src/**"], "nonGoals": ["Unrelated refactors"]},
            "acceptance": [{"text": "Given X, when Y, then Z", "status": "satisfied"}],
            "verification": [{"text": "python -m unittest", "status": "passed", "independence": "self"}],
        },
    )
    write_json(
        task_dir / "STATUS.json",
        {
            "schemaVersion": 1,
            "taskId": "TASK-001",
            "state": state,
            "revision": 3,
            "owner": "alice",
            "nextOwner": "bob",
            "handoffState": None,
            "nextAction": "Run the parity fixture",
        },
    )
    write_json(task_dir / "evidence" / "E-001.json", {"schemaVersion": 1, "evidenceId": "E-001"})
    return task_dir


class TaskViewTests(unittest.TestCase):
    def test_target_bundle_emits_canonical_view_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_task(root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "view", "--project-root", str(root), "--task-id", "TASK-001"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["provider"], "project-to-act")
            self.assertEqual(view["state"], "in_progress")
            self.assertEqual(view["revision"], 3)
            self.assertEqual(view["acceptance"], [{"text": "Given X, when Y, then Z", "status": "satisfied"}])
            self.assertEqual(view["evidenceRefs"], [".project-to-act/tasks/TASK-001/evidence/E-001.json"])
            self.assertTrue(any(gap["field"] == "baseRevision" for gap in view["gaps"]))
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(after, before)

    def test_string_items_are_preserved_with_explicit_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = make_task(root)
            task = json.loads((task_dir / "TASK.json").read_text(encoding="utf-8"))
            task["acceptance"] = ["Observable acceptance"]
            task["verification"] = ["npm test"]
            write_json(task_dir / "TASK.json", task)
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "view", "--project-root", str(root), "--task-id", "TASK-001"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["acceptance"][0]["status"], "unknown")
            self.assertEqual(view["verification"][0]["independence"], "unknown")
            self.assertTrue(any(gap["field"] == "acceptance[0].status" for gap in view["gaps"]))

    def test_role_preview_validates_state_and_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_task(root, state="review")
            payload = root / "payload.json"
            write_json(
                payload,
                {
                    "implementationSummary": "Implemented canonical view",
                    "changedComponents": ["task_view.py"],
                    "changeRefs": ["working-tree"],
                    "designDecisions": [],
                    "deviationsFromContract": [],
                    "selfTestEvidenceRefs": ["E-001"],
                    "setupOrMigrationSteps": [],
                    "knownLimitations": [],
                    "suspectedRiskAreas": [],
                    "requestedTestFocus": ["legacy parity"],
                },
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "role-preview",
                    "--project-root",
                    str(root),
                    "--task-id",
                    "TASK-001",
                    "--handoff-type",
                    "builder_to_verifier.verification_candidate",
                    "--payload-file",
                    str(payload),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads(result.stdout)
            self.assertEqual(preview["writes"], False)
            self.assertEqual(preview["authorizationValidated"], False)
            self.assertEqual(preview["producerRole"], "builder")
            self.assertEqual(preview["consumerRole"], "verifier")
            self.assertEqual(preview["taskState"], "review")

    def test_role_preview_fails_closed_on_wrong_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_task(root, state="in_progress")
            payload = root / "payload.json"
            write_json(payload, {})
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "role-preview",
                    "--project-root",
                    str(root),
                    "--task-id",
                    "TASK-001",
                    "--handoff-type",
                    "builder_to_verifier.verification_candidate",
                    "--payload-file",
                    str(payload),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["code"], "ROLE_STATE_CONFLICT")

    def test_change_request_requires_structured_findings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_task(root, state="review")
            payload = root / "payload.json"
            write_json(payload, {"overallVerdict": "changes-required", "findings": ["not structured"]})
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "role-preview",
                    "--project-root",
                    str(root),
                    "--task-id",
                    "TASK-001",
                    "--handoff-type",
                    "verifier_to_builder.change_request",
                    "--payload-file",
                    str(payload),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["code"], "ROLE_PAYLOAD_INVALID")


if __name__ == "__main__":
    unittest.main()
