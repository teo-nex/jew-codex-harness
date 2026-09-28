"""Integration tests for provider ladder reasoning effort shaping and fallback."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from provider_ladder import Ladder, validate_config


CONFIG = {
    "plus_connection_id": "plus",
    "gemini_connection_ids": ["g1"],
    "gemini_model": "antigravity/gemini-2.5",
    "glm_model": "gonkagate/glm",
    "deepseek_model": "gonkagate/deepseek-r1",
    "main_model": "gpt-6-luna",
    "reasoning_profiles": {
        "antigravity/gemini-2.5": {
            "supported_efforts": ["low", "high"],
            "effort_map": {"xhigh": "high"},
        },
        "gonkagate/deepseek-r1": {
            "supported_efforts": ["high", "max"],
        },
        "gonkagate/glm": {
            "supported": False,
        },
    },
}


class LadderConfigReasoningTests(unittest.TestCase):
    def test_ladder_config_validation_with_reasoning_profiles(self):
        validated = validate_config(CONFIG)
        self.assertIn("reasoning_profiles", validated)
        self.assertIn("antigravity/gemini-2.5", validated["reasoning_profiles"])

    def test_ladder_config_validation_rejects_malformed_profiles(self):
        bad_config = {
            **CONFIG,
            "reasoning_profiles": {"antigravity/gemini-2.5": {"supported_efforts": ["invalid_effort"]}}
        }
        with self.assertRaises(ValueError):
            validate_config(bad_config)

    def test_manual_bypass_unchanged(self):
        """Native/manual routing and non-ladder requests are unchanged."""
        import jev_server
        # apply_route_payload directly preserves manual request parameters
        payload = {"model": "gpt-6-sol", "reasoning": {"effort": "high"}}
        original = dict(payload["reasoning"])
        injected, transport = jev_server.apply_route_payload(payload, "gpt-6-sol", "high", original)
        self.assertIsNone(injected)
        self.assertEqual(payload["reasoning"]["effort"], "high")

    def test_native_none_effort_preserves_client_reasoning(self):
        import jev_server
        original = {"effort": "high", "summary": "auto"}
        payload = {"input": "synthetic", "reasoning": dict(original)}
        jev_server.apply_route_payload(payload, "gpt-6-sol", None, original)
        self.assertEqual(payload["reasoning"], original)


class LadderReasoningIntegrationTests(unittest.TestCase):
    def setUp(self):
        import jev_server
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "ladder_state.json"
        ladder = Ladder(CONFIG, self.state_file)
        for name, replacement in (
            ("_provider_ladder", mock.Mock(return_value=ladder)),
            ("_omniroute_key", mock.Mock(return_value="synthetic")),
            ("log_line", mock.Mock()),
            ("remember_cache_model", mock.Mock()),
            ("remember_route_lease", mock.Mock()),
        ):
            patcher = mock.patch.object(jev_server, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_serve_ladder_preserves_canonical_input_and_retry_uses_original_effort(self):
        """Simulate retry sequence: Gemini -> GLM (unsupported) -> DeepSeek -> Main.

        Verify:
        - Gemini: xhigh maps to high (effort_map)
        - GLM: unsupported profile removes effort from payload reasoning, preserves canonical input
        - DeepSeek: requested original xhigh maps to ceiling/max (max)
        - Main: unconfigured model preserves legacy original effort (xhigh) with status unknown
        - Each attempt does not pollute next attempt with previous model's mapping.
        """
        import jev_server

        config_path = Path(self.temp_dir.name) / "ladder_config.json"
        config_path.write_text(json.dumps(CONFIG))

        with mock.patch.dict("os.environ", {
            "JEV_LADDER_CONFIG": str(config_path),
            "JEV_LADDER_STATE": str(self.state_file),
            "JEV_OMNIROUTE_KEY": "test-key",
        }):
            server_instance = jev_server.Handler.__new__(jev_server.Handler)
            server_instance._attempts = []

            original_payload = {
                "model": "jev/auto",
                "input": [{"role": "user", "content": "solve math"}],
                "reasoning": {"effort": "xhigh", "type": "enabled"},
                "reasoning_effort": "xhigh",  # Stale flat fields should be purged
                "thinking": {"type": "enabled"},
                "prompt_cache_key": "cache-1",
            }

            forward_calls = []

            def fake_forward(payload, out_path, stream_requested, debug, marker, model,
                              signature=None, effective_effort=None, exact_route=False,
                              external=None, validate_gonka=False):
                # Copy payload to record exact shaped payload per attempt
                import copy
                forward_calls.append({
                    "model": model,
                    "effective_effort": effective_effort,
                    "payload": copy.deepcopy(payload),
                    "marker": marker,
                })
                # Simulate 500 error to force ladder to advance to next stage
                attempt = {
                    "model": model,
                    "effort": effective_effort,
                    "status": 500,
                    "terminal_type": None,
                    "completion": "http_error",
                }
                server_instance._attempts.append(attempt)
                return 500, "json", "application/json", False, b'{"error":"fail"}', None

            server_instance._forward = fake_forward
            server_instance.send_response = mock.MagicMock()
            server_instance.send_header = mock.MagicMock()
            server_instance.end_headers = mock.MagicMock()
            server_instance.wfile = mock.MagicMock()

            payload_copy = json.loads(json.dumps(original_payload))
            server_instance._serve_ladder(
                payload=payload_copy,
                scope="test-thread-1",
                base_model="gpt-6-sol",
                effort="xhigh",
                decision={"model": "SOL", "effort": "xhigh", "gate": "auto"},
                step={"step_type": "user_turn", "errored": False, "digest": ""},
                jev_usage=None,
                stream_requested=False,
                debug=False,
            )

            # Check that attempts recorded the correct progression and resolution
            # Stages: plus -> gemini -> glm -> deepseek -> main
            models_called = [call["model"] for call in forward_calls]
            for call in forward_calls:
                self.assertEqual(call["payload"]["input"], original_payload["input"])
                self.assertEqual(call["payload"]["prompt_cache_key"], "cache-1")
            self.assertIn("codex/gpt-5.6-sol-xhigh", models_called)
            self.assertIn("antigravity/gemini-2.5", models_called)
            self.assertIn("gonkagate/glm", models_called)
            self.assertIn("gonkagate/deepseek-r1", models_called)
            self.assertIn("gpt-6-luna", models_called)

            # 1. Plus stage (unconfigured profile -> legacy unknown, keeps xhigh)
            plus_call = next(c for c in forward_calls if c["model"] == "codex/gpt-5.6-sol-xhigh")
            self.assertEqual(plus_call["effective_effort"], "xhigh")
            self.assertEqual(plus_call["payload"]["reasoning"]["effort"], "xhigh")
            self.assertNotIn("reasoning_effort", plus_call["payload"])
            self.assertNotIn("thinking", plus_call["payload"])

            # 2. Gemini stage: antigravity/gemini-2.5 has effort_map {"xhigh": "high"}
            gemini_call = next(c for c in forward_calls if c["model"] == "antigravity/gemini-2.5")
            self.assertEqual(gemini_call["effective_effort"], "high")
            self.assertEqual(gemini_call["payload"]["reasoning"]["effort"], "high")

            # 3. GLM stage: gonkagate/glm has supported=False
            # Should have effective_effort = None, reasoning effort removed, but other reasoning keys preserved
            glm_call = next(c for c in forward_calls if c["model"] == "gonkagate/glm")
            self.assertIsNone(glm_call["effective_effort"])
            self.assertNotIn("effort", glm_call["payload"]["reasoning"])
            self.assertEqual(glm_call["payload"]["reasoning"].get("type"), "enabled")

            # 4. DeepSeek stage: gonkagate/deepseek-r1 supports [high, max]
            # Must resolve from ORIGINAL requested effort xhigh, NOT Gemini's high or GLM's None!
            # xhigh ceiling on [high, max] -> max
            deepseek_call = next(c for c in forward_calls if c["model"] == "gonkagate/deepseek-r1")
            self.assertEqual(deepseek_call["effective_effort"], "max")
            self.assertEqual(deepseek_call["payload"]["reasoning"]["effort"], "max")

            # 5. Main stage: gpt-6-luna (unconfigured profile)
            # Preserves original requested effort xhigh
            main_call = next(c for c in forward_calls if c["model"] == "gpt-6-luna")
            self.assertEqual(main_call["effective_effort"], "xhigh")
            self.assertEqual(main_call["payload"]["reasoning"]["effort"], "xhigh")

            # Verify telemetry on attempts has requested_effort, effective_effort, reasoning_source
            for att in server_instance._attempts:
                if att["model"] == "codex/gpt-5.6-sol-xhigh":
                    self.assertEqual(att["requested_effort"], "xhigh")
                    self.assertEqual(att["effective_effort"], "xhigh")
                    self.assertEqual(att["reasoning_status"], "unknown")
                    self.assertEqual(att["reasoning_source"], "legacy_unknown")
                elif att["model"] == "antigravity/gemini-2.5":
                    self.assertEqual(att["requested_effort"], "xhigh")
                    self.assertEqual(att["effective_effort"], "high")
                    self.assertEqual(att["reasoning_status"], "mapped")
                    self.assertEqual(att["reasoning_source"], "effort_map")
                elif att["model"] == "gonkagate/glm":
                    self.assertEqual(att["requested_effort"], "xhigh")
                    self.assertIsNone(att["effective_effort"])
                    self.assertEqual(att["reasoning_status"], "unsupported")
                    self.assertEqual(att["reasoning_source"], "unsupported_profile")
                elif att["model"] == "gonkagate/deepseek-r1":
                    self.assertEqual(att["requested_effort"], "xhigh")
                    self.assertEqual(att["effective_effort"], "max")
                    self.assertEqual(att["reasoning_status"], "mapped")
                    self.assertEqual(att["reasoning_source"], "ladder_ceiling")
                elif att["model"] == "gpt-6-luna":
                    self.assertEqual(att["requested_effort"], "xhigh")
                    self.assertEqual(att["effective_effort"], "xhigh")
                    self.assertEqual(att["reasoning_status"], "unknown")
                    self.assertEqual(att["reasoning_source"], "legacy_unknown")

    def test_serve_ladder_with_classifier_effort_none_uses_original_nested_effort(self):
        """When classifier effort is None, _serve_ladder must use original nested effort."""
        import jev_server

        config_path = Path(self.temp_dir.name) / "ladder_config.json"
        config_path.write_text(json.dumps(CONFIG))

        with mock.patch.dict("os.environ", {
            "JEV_LADDER_CONFIG": str(config_path),
            "JEV_LADDER_STATE": str(self.state_file),
            "JEV_OMNIROUTE_KEY": "test-key",
        }):
            server_instance = jev_server.Handler.__new__(jev_server.Handler)
            server_instance._attempts = []

            payload = {
                "model": "jev/auto",
                "input": [{"role": "user", "content": "solve math"}],
                "reasoning": {"effort": "xhigh"},
                "prompt_cache_key": "cache-none-test",
            }

            forward_calls = []

            def fake_forward(payload, out_path, stream_requested, debug, marker, model,
                              signature=None, effective_effort=None, exact_route=False,
                              external=None, validate_gonka=False):
                forward_calls.append({"model": model, "effective_effort": effective_effort})
                attempt = {
                    "model": model,
                    "effort": effective_effort,
                    "status": 200,
                    "terminal_type": "response.completed",
                    "completion": "response.completed",
                }
                server_instance._attempts.append(attempt)
                return 200, "json", "application/json", False, None, None

            server_instance._forward = fake_forward
            server_instance.send_response = mock.MagicMock()
            server_instance.send_header = mock.MagicMock()
            server_instance.end_headers = mock.MagicMock()
            server_instance.wfile = mock.MagicMock()

            # Pass effort=None from classifier
            server_instance._serve_ladder(
                payload=payload,
                scope="test-thread-none",
                base_model="gpt-6-sol",
                effort=None,
                decision={"model": "SOL", "effort": None, "gate": "auto"},
                step={"step_type": "user_turn", "errored": False, "digest": ""},
                jev_usage=None,
                stream_requested=False,
                debug=False,
            )

            # Plus model receives routed effort "xhigh" from original reasoning
            self.assertEqual(len(forward_calls), 1)
            self.assertEqual(forward_calls[0]["model"], "codex/gpt-5.6-sol-xhigh")
            self.assertEqual(forward_calls[0]["effective_effort"], "xhigh")
            att = server_instance._attempts[0]
            self.assertEqual(att["requested_effort"], "xhigh")
            self.assertEqual(att["effective_effort"], "xhigh")
            self.assertEqual(att["reasoning_source"], "legacy_unknown")


if __name__ == "__main__":
    unittest.main()
