"""The Node Jev transport uses only pinned decision endpoints."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


HELPER = Path(__file__).with_name("jev_fetch.mjs")
NODE = shutil.which("node")
MOCK_FETCH = """
import { pathToFileURL } from 'node:url';
globalThis.fetch = async (url, options) => {
  process.stderr.write('endpoint=' + url + '\\n');
  const body = JSON.parse(options.body);
  return { ok: true, json: async () => ({
    model: body.model,
    answers: { choice: { type: 'choice', choice: 'open', confidence: 1 } },
  }) };
};
await import(pathToFileURL(process.argv[1]).href);
"""


@unittest.skipUnless(NODE, "Node.js is unavailable")
class JevFetchNodeTests(unittest.TestCase):
    def call(self, provider):
        payload = {"provider": provider, "key": "synthetic-key",
                   "body": {"model": "typesafe/jev-1.13", "state": "synthetic",
                            "questions": {"choice": {"type": "choice"}}}}
        return subprocess.run([NODE, "--input-type=module", "-e", MOCK_FETCH, str(HELPER)],
                              input=json.dumps(payload), capture_output=True, text=True, check=False)

    def test_openrouter_uses_decisions_api(self):
        result = self.call("openrouter")
        self.assertEqual(result.returncode, 0)
        self.assertIn("endpoint=https://openrouter.ai/api/alpha/decisions", result.stderr)
        self.assertEqual(json.loads(result.stdout)["answers"]["choice"]["choice"], "open")
        self.assertNotIn("synthetic-key", result.stderr + result.stdout)

    def test_typesafe_uses_direct_system_one_api(self):
        result = self.call("typesafe")
        self.assertEqual(result.returncode, 0)
        self.assertIn("endpoint=https://api.typesafe.ai/v1/systemone", result.stderr)

    def test_unknown_provider_cannot_select_an_endpoint(self):
        result = self.call("__proto__")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("endpoint=", result.stderr)


if __name__ == "__main__":
    unittest.main()
