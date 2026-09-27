import tempfile
import unittest
from pathlib import Path

from harness.agent_config import merge_agents, remove_agents


REPO = Path(__file__).resolve().parents[1]


class AgentConfigTests(unittest.TestCase):
    def test_round_trip_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = "# Existing\n\n- Keep this.  \n"
            merged = merge_agents(original, REPO, home)
            self.assertEqual(merge_agents(merged, REPO, home), merged)
            self.assertIn(str(home / "jev-global/ui.mjs"), merged)
            self.assertEqual(remove_agents(merged, REPO, home), original)

    def test_changed_managed_section_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            altered = merge_agents("", REPO, home).replace("safeBrowserClick", "rawClick")
            with self.assertRaisesRegex(ValueError, "differs"):
                merge_agents(altered, REPO, home)
            with self.assertRaisesRegex(ValueError, "changed"):
                remove_agents(altered, REPO, home)

    def test_unrelated_text_before_and_after_managed_section_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            merged = merge_agents("# Existing\n", REPO, home)
            updated = "New preface\n" + merged + "Unrelated footer\n"
            self.assertEqual(merge_agents(updated, REPO, home), updated)
            self.assertEqual(remove_agents(updated, REPO, home),
                             "New preface\n# Existing\nUnrelated footer\n")


if __name__ == "__main__":
    unittest.main()
