import tempfile
import unittest
from pathlib import Path
import tomllib

from harness.browser_config import merge_browser_config


class BrowserConfigTests(unittest.TestCase):
    def test_additive_and_idempotent_when_helper_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "mcp-helper.mjs"
            helper.write_text("// helper")
            before = '[mcp_servers.other]\ncommand = "other"\n'
            once = merge_browser_config(before, root, "/bin/node", helper)
            self.assertEqual(merge_browser_config(once, root, "/bin/node", helper), once)
            value = tomllib.loads(once)["mcp_servers"]
            self.assertEqual(value["other"]["command"], "other")
            self.assertEqual(value["jev-browser"]["args"],
                             [str(root / "integrations/jev/browser_broker/mcp.mjs")])

    def test_missing_helper_and_foreign_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(merge_browser_config("", root, "node", root / "missing"), "")
            helper = root / "helper.mjs"
            helper.touch()
            with self.assertRaisesRegex(ValueError, "migration"):
                merge_browser_config('[mcp_servers.jev-browser]\ncommand = "foreign"\n',
                                     root, "node", helper)


if __name__ == "__main__":
    unittest.main()
