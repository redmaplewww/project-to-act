import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "project-to-act"
LEGACY_SLUG = "protect" + "-to-act"
LEGACY_TITLE = "Protect" + " to Act"


class SkillIdentityTests(unittest.TestCase):
    def test_skill_folder_and_metadata_use_project_to_act(self):
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file())
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata_text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn("name: project-to-act", skill_text)
        self.assertIn("# Project to Act", skill_text)
        self.assertIn('display_name: "Project to Act"', metadata_text)
        self.assertIn("$project-to-act", metadata_text)

    def test_distribution_contains_no_legacy_name(self):
        candidates = [
            path
            for path in REPO_ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.suffix.lower() in {".md", ".yaml", ".py"}
        ]
        for path in candidates:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(LEGACY_SLUG, text, str(path))
            self.assertNotIn(LEGACY_TITLE, text, str(path))

    def test_thin_team_handoff_fixture_is_test_only_and_owns_no_runtime(self):
        fixture = REPO_ROOT / "tests" / "fixtures" / "team-handoff" / "SKILL.md"
        self.assertTrue(fixture.is_file())
        text = fixture.read_text(encoding="utf-8")
        self.assertIn("Test-only", text)
        self.assertIn("owns no schema, state, runtime, hook, event store, or writer", text)
        self.assertFalse((REPO_ROOT / "project-to-act" / "team-handoff").exists())

    def test_runtime_is_project_to_act_owned_and_explicitly_gated(self):
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        runtime_text = (SKILL_ROOT / "scripts" / "task_runtime.py").read_text(encoding="utf-8")
        self.assertIn("references/runtime-contract.md", skill_text)
        self.assertIn("experimentalHandoffWrites", runtime_text)
        self.assertIn("ENABLED_HANDOFF_TYPE", runtime_text)
        self.assertTrue((SKILL_ROOT / "scripts" / "runtime_transaction.py").is_file())


if __name__ == "__main__":
    unittest.main()
