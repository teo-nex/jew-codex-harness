"""GitHub-hosted Windows verifies Task Scheduler's XML round trip."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from harness.platforms import windows


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
    "only on a disposable GitHub-hosted Windows runner",
)
class LiveTaskXml(unittest.TestCase):
    def test_scheduler_preserves_installer_owned_fields(self):
        with tempfile.TemporaryDirectory(prefix="jev-task-xml-") as scratch:
            root = Path(scratch)
            (root / "server").mkdir()
            (root / "server/jev_server.py").write_text("# fixture\n", encoding="utf-8")
            plan = windows.plan_service(root, root / "codex", root / "state",
                                        {"JEV_LISTEN_PORT": "54323"})
            definition = root / "task.xml"
            definition.write_text(windows._render(plan), encoding="utf-8")
            task_name = "JevCodexHarnessXmlSmoke" + str(os.getpid())
            created = False
            try:
                subprocess.run(["schtasks", "/Create", "/TN", task_name,
                                "/XML", str(definition), "/RL", "LIMITED"], check=True,
                               capture_output=True, timeout=30)
                created = True
                query = subprocess.run(["schtasks", "/Query", "/TN", task_name,
                                        "/XML"], check=True, capture_output=True,
                                       text=True, timeout=30)
                if not windows._owned_xml(query.stdout, plan):
                    root_xml = ET.fromstring(query.stdout)
                    expected = {"Description": plan["marker"],
                                "UserId": plan["owner_sid"],
                                "LogonType": "InteractiveToken",
                                "RunLevel": "LeastPrivilege",
                                "Command": plan["command"][0],
                                "Arguments": subprocess.list2cmdline([plan["wrapper_path"]]),
                                "WorkingDirectory": plan["repo"]}
                    matches = {}
                    for key, value in expected.items():
                        element = root_xml.find(f".//{{{windows.NS}}}{key}")
                        matches[key] = element is not None and element.text == value
                    run_level = root_xml.find(f".//{{{windows.NS}}}RunLevel")
                    self.fail(f"Task Scheduler changed installer-owned fields: {matches}; "
                              f"RunLevel={run_level.text if run_level is not None else None!r}")
            finally:
                if created:
                    subprocess.run(["schtasks", "/Delete", "/TN", task_name,
                                    "/F"], check=True, capture_output=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
