"""User-owned provider order, recovery and canonical reasoning contracts."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, NATIVE_MODELS, plus_model, validate_config
from reasoning_effort import validate_reasoning_profiles


CONFIG = {"providers": [
    {"id": "first", "model": "vendor/model-a", "connection_ids": ["a1", "a2"]},
    {"id": "second", "model": "vendor/model-b"},
    {"id": "native", "transport": "native"},
]}


class OrderedLadderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"

    def test_custom_order_needs_no_plus_or_gemini_and_accounts_are_sticky(self):
        ladder = Ladder(CONFIG, self.path)
        seen = []
        for _ in range(ladder.attempt_limit):
            route = ladder.route("thread", "gpt-6-astra", "xhigh")
            self.assertEqual(route, Ladder(CONFIG, self.path).route("thread", "gpt-6-astra", "xhigh"))
            seen.append((route["stage"], route["account"], route["model"]))
            self.assertTrue(ladder.advance("thread", route, "quota"))
            self.assertFalse(ladder.advance("thread", route, "quota"))
        self.assertEqual(seen, [("first", "a1", "vendor/model-a"),
                                ("first", "a2", "vendor/model-a"),
                                ("second", None, "vendor/model-b"),
                                ("native", None, "gpt-6-astra")])
        self.assertEqual(ladder.route("thread", "gpt-6-astra", "xhigh")["stage"], "exhausted")

    def test_explicit_model_mapping_preserves_exact_destination(self):
        config = {"providers": [{"id": "mapped", "models": {
            model: "gateway/" + model for model in NATIVE_MODELS}}]}
        ladder = Ladder(config, self.path)
        for model in NATIVE_MODELS:
            self.assertEqual(ladder.route("thread", model, "max")["model"], "gateway/" + model)

    def test_validated_order_is_isolated_from_mutations_of_the_callers_config(self):
        config = copy.deepcopy(CONFIG)
        ladder = Ladder(config, self.path)
        config["providers"][0]["connection_ids"].reverse()
        self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["account"], "a1")
        mapped = {"providers": [{"id": "mapped", "models": {m: "gateway/" + m for m in NATIVE_MODELS}}]}
        ladder = Ladder(mapped, self.path)
        mapped["providers"][0]["models"]["gpt-6-sol"] = "unexpected"
        self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["model"], "gateway/gpt-6-sol")

    def test_legacy_astra_does_not_silently_downgrade_to_sol(self):
        legacy = {"plus_connection_id": "plus", "gemini_connection_ids": ["g1"],
                  "gemini_model": "gemini", "glm_model": "glm",
                  "deepseek_model": "deepseek", "main_model": "gpt-6-luna"}
        ladder = Ladder(legacy, self.path)
        for effort in ("low", "medium", "high", "xhigh", "max"):
            route = ladder.route("thread", "gpt-6-astra", effort)
            self.assertEqual((route["model"], route["transport"]), ("gpt-6-astra", "native"))
            self.assertEqual(plus_model("gpt-6-astra", effort), "gpt-6-astra")
        with self.assertRaises(ValueError):
            plus_model("unknown", "high")
        with self.assertRaises(ValueError):
            validate_config({**legacy, "gemini_connection_ids": [{}]})
        with self.assertRaises(ValueError):
            validate_config({**legacy, "opus_model": "antigravity/claude-opus-test",
                             "opus_connection_ids": [{}]})

    def test_reorder_invalidates_old_state_and_old_failure(self):
        old = Ladder(CONFIG, self.path)
        route = old.route("thread", "gpt-6-sol", "high")
        old.advance("thread", route, "quota")
        new = Ladder({"providers": list(reversed(CONFIG["providers"]))}, self.path)
        self.assertEqual(new.route("thread", "gpt-6-sol", "high")["stage"], "native")
        self.assertFalse(new.advance("thread", route, "quota"))
        self.assertFalse(old.note_success("thread", route))

    def test_primary_recovers_and_stale_failure_cannot_break_probe(self):
        config = {"providers": [{"id": "first", "model": "a"}, {"id": "second", "model": "b"}]}
        ladder = Ladder(config, self.path)
        with mock.patch("provider_ladder.time.time", return_value=1000):
            failed = ladder.route("thread", "gpt-6-sol", "high")
            ladder.advance("thread", failed, "quota", reset_at=1100)
            self.assertEqual(ladder.route("new-thread", "gpt-6-sol", "high")["stage"], "second")
        with mock.patch("provider_ladder.time.time", return_value=1101):
            probe = ladder.route("thread", "gpt-6-sol", "high")
            self.assertEqual(probe["stage"], "first")
            self.assertFalse(ladder.advance("thread", failed, "quota"))
            self.assertTrue(ladder.note_success("thread", probe))
            self.assertEqual(ladder.route("fresh-thread", "gpt-6-sol", "high")["stage"], "first")

    def test_single_provider_can_recover_after_exhaustion(self):
        ladder = Ladder({"providers": [{"id": "only", "model": "a"}]}, self.path)
        with mock.patch("provider_ladder.time.time", return_value=1000):
            ladder.advance("thread", ladder.route("thread", "gpt-6-sol", "high"), "unavailable", cooldown_seconds=30)
            self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["stage"], "exhausted")
        with mock.patch("provider_ladder.time.time", return_value=1031):
            self.assertEqual(ladder.route("thread", "gpt-6-sol", "high")["stage"], "only")

    def test_invalid_config_is_rejected_before_runtime(self):
        cases = [[], [{"id": "a"}], [{"id": "a", "model": " x "}],
                 [{"id": "exhausted", "model": "a"}],
                 [{"id": "a", "model": "a"}, {"id": "a", "model": "b"}],
                 [{"id": "a", "models": {"gpt-6-luna": "a"}}],
                 [{"id": "a", "model": "a", "connection_ids": [{}]}],
                 [{"id": "a", "transport": "native", "connection_ids": ["a"]}],
                 [{"id": "a", "model": "a", "extra": True}]]
        for providers in cases:
            with self.subTest(providers=providers), self.assertRaises(ValueError):
                validate_config({"providers": providers})
        with self.assertRaises(ValueError):
            validate_config({**CONFIG, "plus_connection_id": "a"})
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({"model-a ": {"supported": False}})

    def test_corrupt_recovery_state_fails_closed_without_restarting_accounts(self):
        ladder = Ladder(CONFIG, self.path)
        initial = {"config_hash": ladder.fingerprint, "provider_index": 0, "account_index": 0}
        cases = [{"provider_index": None}, {"generation": True},
                 {"primary_retry_at": "tomorrow"}, {"primary_retry_at": float("nan")},
                 {"resume_route": [1, 9]}, {"resume_route": [0, 0]}]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                self.path.write_text(json.dumps({"thread": {**initial, **invalid}}))
                with self.assertRaises(ValueError):
                    ladder.route("thread", "gpt-6-sol", "high")
        for circuit in ([], {"config_hash": ladder.fingerprint, "until": "tomorrow"}):
            self.path.write_text(json.dumps({"thread": initial, "_ordered_primary_circuit": circuit}))
            with self.assertRaises(ValueError):
                ladder.route("thread", "gpt-6-sol", "high")


class OrderedForwardTests(unittest.TestCase):
    def _run(self, config, outcomes, *, tool_search=False, advance_time=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        ladder = Ladder(config, Path(temporary.name) / "state.json")
        handler = jev.Handler.__new__(jev.Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._json = mock.Mock()
        calls = []
        def forward(payload, *args, **kwargs):
            status, unwritten = outcomes[len(calls)]
            calls.append((copy.deepcopy(payload), kwargs))
            handler._attempts.append({"status": status, "model": payload["model"],
                                      "terminal_type": "response.completed" if status == 200 else None})
            if advance_time is not None:
                advance_time()
            return status, "json", "application/json", status == 429, unwritten, None
        handler._forward = forward
        original = {"model": "jev/auto", "input": [{"role": "user", "content": "synthetic"}],
                    "reasoning": {"effort": "xhigh", "summary": "auto"}, "prompt_cache_key": "stable"}
        if tool_search:
            original["input"].append({"type": "tool_search_output", "tools": []})
        with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
             mock.patch.object(jev, "_omniroute_key", return_value="synthetic"), \
             mock.patch.object(jev, "log_line"), \
             mock.patch.object(jev, "remember_cache_model"), \
             mock.patch.object(jev, "remember_route_lease"):
            handler._serve_ladder(copy.deepcopy(original), "scope", jev.SOL, "xhigh", None,
                                  {"step_type": "user_turn"}, None, True, False)
        return calls, handler, original

    def test_retry_resolves_original_effort_and_native_can_precede_external(self):
        config = {"providers": [{"id": "local", "transport": "native"},
                                {"id": "b", "model": "b"}, {"id": "c", "model": "c"}],
                  "reasoning_profiles": {"b": {"supported": False},
                                         "c": {"supported_efforts": ["low", "high"]}}}
        calls, handler, original = self._run(config, [(503, b"error"), (503, b"error"), (200, None)])
        self.assertEqual([p["model"] for p, _ in calls], ["gpt-6-sol", "b", "c"])
        self.assertIsNone(calls[0][1]["external"])
        self.assertIsNotNone(calls[1][1]["external"])
        self.assertEqual([p["reasoning"].get("effort") for p, _ in calls], ["xhigh", None, "high"])
        for payload, _ in calls:
            self.assertEqual(payload["input"], original["input"])
            self.assertEqual(payload["prompt_cache_key"], "stable")
            self.assertEqual(payload["reasoning"]["summary"], "auto")
        handler._json.assert_not_called()

    def test_more_than_five_custom_routes_are_attempted_in_exact_order(self):
        config = {"providers": [{"id": f"p{i}", "model": f"m{i}"} for i in range(9)]}
        calls, _, _ = self._run(config, [(503, b"error")] * 8 + [(200, None)])
        self.assertEqual([p["model"] for p, _ in calls], [f"m{i}" for i in range(9)])

    def test_slow_fallbacks_do_not_probe_primary_again_in_the_same_request(self):
        clock = [1000]
        def advance_time():
            clock[0] += 40
        config = {"providers": [{"id": f"p{i}", "model": f"m{i}"} for i in range(3)]}
        with mock.patch("provider_ladder.time.time", side_effect=lambda: clock[0]):
            calls, _, _ = self._run(config, [(503, b"error"), (503, b"error"), (200, None)],
                                    advance_time=advance_time)
        self.assertEqual([p["model"] for p, _ in calls], ["m0", "m1", "m2"])

    def test_exposed_response_is_never_replayed(self):
        calls, _, _ = self._run(CONFIG, [(502, None)])
        self.assertEqual(len(calls), 1)

    def test_tool_search_requires_explicit_native_provider(self):
        calls, handler, _ = self._run({"providers": [{"id": "only", "model": "a"}]}, [], tool_search=True)
        self.assertEqual(calls, [])
        self.assertEqual(handler._json.call_args.args[0], 400)
        calls, _, _ = self._run(CONFIG, [(200, None)], tool_search=True)
        self.assertEqual([p["model"] for p, _ in calls], ["gpt-6-sol"])

    def test_failed_forced_native_is_attempted_once(self):
        calls, _, _ = self._run(CONFIG, [(503, b"error")], tool_search=True)
        self.assertEqual(len(calls), 1)

    def test_corrupt_state_returns_a_controlled_error_before_forwarding(self):
        with mock.patch.object(Ladder, "route", side_effect=ValueError("synthetic corruption")):
            calls, handler, _ = self._run(CONFIG, [])
        self.assertEqual(calls, [])
        self.assertEqual(handler._json.call_args.args[0], 503)
        self.assertEqual(handler._json.call_args.args[1]["error"]["type"], "ValueError")
