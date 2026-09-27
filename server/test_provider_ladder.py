"""Provider priority must preserve one thread and advance only on failed routes."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from provider_ladder import Ladder, plus_model


CONFIG = {"plus_connection_id": "plus", "gemini_connection_ids": ["g1", "g2", "g3"],
          "gemini_model": "antigravity/gemini", "glm_model": "gonkagate/glm",
          "deepseek_model": "gonkagate/deepseek", "main_model": "gpt-6-luna"}


class LadderTests(unittest.TestCase):
    def test_wally_follows_gonka_deepseek_and_precedes_main(self):
        config = {**CONFIG, "wally_model": "wally/glm-5.3-flash"}
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(config, Path(directory) / "state.json")
            seen = []
            for _ in range(8):
                route = ladder.route("thread", "gpt-6-luna", "low")
                seen.append((route["stage"], route["model"]))
                ladder.advance("thread", route, "quota")
            self.assertEqual(seen[-4:], [("glm", "gonkagate/glm"),
                                         ("deepseek", "gonkagate/deepseek"),
                                         ("wally", "wally/glm-5.3-flash"),
                                         ("main", "gpt-6-luna")])

    def test_existing_main_thread_gets_one_wally_probe_after_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            old = Ladder(CONFIG, path)
            for _ in range(1 + len(CONFIG["gemini_connection_ids"]) + 2):
                route = old.route("existing", "gpt-6-luna", "low")
                old.advance("existing", route, "quota")
            self.assertEqual(old.route("existing", "gpt-6-luna", "low")["stage"], "main")
            upgraded = Ladder({**CONFIG, "wally_model": "wally/glm-5.3-flash"}, path)
            route = upgraded.route("existing", "gpt-6-luna", "low")
            self.assertEqual(route["stage"], "wally")
            upgraded.advance("existing", route, "invalid_response")
            self.assertEqual(upgraded.route("existing", "gpt-6-luna", "low")["stage"], "main")

    def test_opus_uses_its_separate_sticky_account_pool_after_gemini(self):
        config = {**CONFIG, "opus_model": "antigravity/claude-opus-4-6-thinking",
                  "opus_connection_ids": ["g1", "g2", "g3"]}
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(config, Path(directory) / "state.json")
            ladder.advance("thread", ladder.route("thread", "gpt-6-luna", "low"), "quota")
            for account in ("g1", "g2", "g3"):
                route = ladder.route("thread", "gpt-6-luna", "low")
                self.assertEqual((route["stage"], route["account"]), ("gemini", account))
                ladder.advance("thread", route, "quota")
            for account in ("g1", "g2", "g3"):
                route = ladder.route("thread", "gpt-6-sol", "high")
                self.assertEqual((route["stage"], route["account"], route["model"]),
                                 ("opus", account, "antigravity/claude-opus-4-6-thinking"))
                self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["account"], account)
                ladder.advance("thread", route, "quota")
            self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["stage"], "glm")

    def test_plus_jev_model_and_sticky_sequential_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            ladder = Ladder(CONFIG, path)
            route = ladder.route("thread-a", "gpt-6-sol", "xhigh")
            self.assertEqual((route["stage"], route["model"], route["account"]),
                             ("plus", "codex/gpt-5.6-sol-xhigh", "plus"))
            self.assertEqual(ladder.route("thread-a", "gpt-6-luna", "low")["stage"], "plus")
            self.assertTrue(ladder.advance("thread-a", route, "quota"))
            for account in ("g1", "g2", "g3"):
                route = ladder.route("thread-a", "gpt-6-luna", "low")
                self.assertEqual((route["stage"], route["account"]), ("gemini", account))
                self.assertEqual(Ladder(CONFIG, path).route("thread-a", "gpt-6-sol", "high")["account"], account)
                self.assertTrue(ladder.advance("thread-a", route, "quota"))
            for stage, model in (("glm", "gonkagate/glm"), ("deepseek", "gonkagate/deepseek"),
                                 ("main", "gpt-6-luna")):
                route = ladder.route("thread-a", "gpt-6-sol", "high")
                self.assertEqual((route["stage"], route["model"]), (stage, model))
                self.assertTrue(ladder.advance("thread-a", route, "unavailable"))
            self.assertEqual(ladder.route("thread-a", "gpt-6-luna", "low")["stage"], "exhausted")
            self.assertEqual(ladder.route("thread-b", "gpt-6-luna", "low")["stage"], "gemini")

    def test_stale_failure_cannot_skip_another_account(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            plus = ladder.route("thread", "gpt-6-luna", "low")
            self.assertTrue(ladder.advance("thread", plus, "quota"))
            first = ladder.route("thread", "gpt-6-luna", "low")
            self.assertTrue(ladder.advance("thread", first, "quota"))
            self.assertFalse(ladder.advance("thread", first, "quota"))
            self.assertEqual(ladder.route("thread", "gpt-6-luna", "low")["account"], "g2")

    def test_model_mapping_and_invalid_config(self):
        self.assertEqual(plus_model("gpt-6-luna", "low"), "codex/gpt-5.6-luna-low")
        self.assertEqual(plus_model("gpt-5.6-terra", "medium"), "codex/gpt-5.6-terra-medium")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                Ladder({**CONFIG, "gemini_connection_ids": ["g1", "g1"]}, Path(directory) / "state.json")
            with self.assertRaises(ValueError):
                Ladder({**CONFIG, "wally_model": "gonkagate/glm"}, Path(directory) / "state.json")

    def test_plus_recovers_after_reset_without_losing_sticky_gemini(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                plus = ladder.route("thread", "gpt-6-luna", "low")
                self.assertTrue(ladder.advance("thread", plus, "quota", reset_at=1100))
                self.assertEqual(ladder.route("thread", "gpt-6-luna", "low")["account"], "g1")
            with mock.patch("provider_ladder.time.time", return_value=1101):
                self.assertEqual(ladder.route("thread", "gpt-6-luna", "low")["stage"], "plus")
                failed_probe = ladder.route("thread", "gpt-6-luna", "low")
                self.assertTrue(ladder.advance("thread", failed_probe, "quota", reset_at=2000))
                self.assertEqual(ladder.route("thread", "gpt-6-luna", "low")["account"], "g1")
            with mock.patch("provider_ladder.time.time", return_value=2001):
                self.assertEqual(ladder.route("thread", "gpt-6-luna", "low")["stage"], "plus")

    def test_corrupt_state_fails_closed_instead_of_resetting_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                Ladder(CONFIG, path).route("thread", "gpt-6-luna", "low")

    def test_plus_failure_opens_shared_circuit_and_success_closes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                failed = ladder.route("a", "gpt-6-sol", "high")
                self.assertTrue(ladder.advance("a", failed, "unavailable", cooldown_seconds=30))
                self.assertEqual(ladder.route("b", "gpt-6-sol", "high")["stage"], "gemini")
            with mock.patch("provider_ladder.time.time", return_value=1031):
                recovered = ladder.route("a", "gpt-6-sol", "high")
                self.assertEqual(recovered["stage"], "plus")
                self.assertTrue(ladder.note_success("a", recovered))
                self.assertEqual(ladder.route("c", "gpt-6-luna", "low")["stage"], "plus")


if __name__ == "__main__":
    unittest.main()
