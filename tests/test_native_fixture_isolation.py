"""Synthetic native fixtures must not launch unrelated plugin marketplace work."""

import os
from pathlib import Path
import tomllib
import unittest
from unittest import mock

import idle_compact_acceptance as idle_fixture
import worker_client_acceptance as worker_fixture


class FixtureProbeComplete(Exception):
    pass


class NativeFixtureIsolationTests(unittest.TestCase):
    def assert_plugins_disabled(self):
        home = Path(os.environ["CODEX_HOME"])
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        self.assertIs(config.get("features", {}).get("plugins"), False)

    def test_idle_profile_disables_unrelated_plugin_clone(self):
        async def inspect(executable, work, model, requests):
            self.assert_plugins_disabled()
            requests.append({"input": "Return the remembered fact IDLE_FACT_42"})
            return {"ok": True}

        result = idle_fixture.run_case("unused", {}, "fixture", inspect)
        self.assertTrue(result["fact_carried"])

    def test_worker_profile_disables_plugins_before_native_launch(self):
        def inspect(task):
            self.assert_plugins_disabled()
            raise FixtureProbeComplete

        with mock.patch.object(worker_fixture.portable, "submit", side_effect=inspect):
            with self.assertRaises(FixtureProbeComplete):
                worker_fixture.run_case("outside", "unused", {"models": []})


if __name__ == "__main__":
    unittest.main()
