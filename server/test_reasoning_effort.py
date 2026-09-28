"""Unit tests for capability-aware reasoning effort routing."""
import unittest

from reasoning_effort import (
    CANONICAL_EFFORTS,
    resolve_model_reasoning_effort,
    validate_reasoning_profiles,
)


class ReasoningEffortValidationTests(unittest.TestCase):
    def test_valid_profiles(self):
        profiles = {
            "model-a": {
                "supported_efforts": ["low", "medium", "high"],
                "effort_map": {"xhigh": "high", "max": "high", "minimal": "low"},
            },
            "model-b": {
                "supported": False,
            },
            "model-c": {
                "supported_efforts": ["none", "max"],
            },
        }
        validated = validate_reasoning_profiles(profiles)
        self.assertEqual(len(validated), 3)
        self.assertEqual(validated["model-b"], {"supported": False})
        self.assertEqual(validated["model-a"]["supported_efforts"], ["low", "medium", "high"])
        self.assertEqual(validated["model-c"]["effort_map"], {})

    def test_invalid_profiles_not_dict(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles("not-a-dict")
        with self.assertRaises(ValueError):
            validate_reasoning_profiles(None)

    def test_invalid_model_id_key(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({"": {"supported": False}})
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({"   ": {"supported": False}})
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({123: {"supported": False}})

    def test_effort_map_null_or_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported_efforts": ["low"], "effort_map": None}
            })
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported_efforts": ["low"], "effort_map": "bad"}
            })

    def test_nonthinking_only_requires_explicit_map_for_positive_efforts(self):
        # Only nonthinking levels supported without effort_map covering all positive efforts -> raises ValueError
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "nonthinking-model": {"supported_efforts": ["none", "minimal"]}
            })
        # With full explicit effort_map covering positive efforts, it validates successfully
        valid = validate_reasoning_profiles({
            "nonthinking-model": {
                "supported_efforts": ["none"],
                "effort_map": {
                    "low": "none",
                    "medium": "none",
                    "high": "none",
                    "xhigh": "none",
                    "max": "none",
                }
            }
        })
        self.assertIn("nonthinking-model", valid)

    def test_unknown_keys_rejected(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported_efforts": ["low"], "extra_field": True}
            })

    def test_duplicate_supported_efforts(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported_efforts": ["low", "low"]}
            })

    def test_invalid_effort_enum(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported_efforts": ["low", "ultra"]}
            })

    def test_unsupported_with_extra_fields(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported": False, "supported_efforts": ["low"]}
            })
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {"supported": True}
            })

    def test_effort_map_target_must_be_supported(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {
                    "supported_efforts": ["low"],
                    "effort_map": {"high": "medium"},  # medium not in supported_efforts
                }
            })

    def test_effort_map_invalid_source_or_target_enum(self):
        with self.assertRaises(ValueError):
            validate_reasoning_profiles({
                "model-a": {
                    "supported_efforts": ["low"],
                    "effort_map": {"bad_source": "low"},
                }
            })


class ReasoningEffortResolutionTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "antigravity/gemini": {
                "supported_efforts": ["low", "high"],
                "effort_map": {"medium": "low"},
            },
            "antigravity/claude-opus-4-6-thinking": {
                "supported_efforts": ["low", "medium", "high", "max"],
            },
            "unsupported-model": {
                "supported": False,
            },
            "ceiling-test-model": {
                "supported_efforts": ["minimal", "high", "max"],
            },
        }

    def test_exact_matching(self):
        res = resolve_model_reasoning_effort("antigravity/gemini", "low", self.profiles)
        self.assertEqual(res, {
            "requested_effort": "low",
            "effective_effort": "low",
            "status": "exact",
            "source": "exact",
        })

    def test_effort_map(self):
        res = resolve_model_reasoning_effort("antigravity/gemini", "medium", self.profiles)
        self.assertEqual(res, {
            "requested_effort": "medium",
            "effective_effort": "low",
            "status": "mapped",
            "source": "effort_map",
        })

    def test_monotonic_mapping_ceiling_then_max(self):
        # ceiling-test-model supports [minimal, high, max]
        # None requested:
        res_none = resolve_model_reasoning_effort("ceiling-test-model", None, self.profiles)
        self.assertEqual(res_none, {
            "requested_effort": None,
            "effective_effort": None,
            "status": "exact",
            "source": "exact",
        })

        # "low" requested -> ceiling is "high" (rank minimal(1) < low(2) <= high(4))
        res_low = resolve_model_reasoning_effort("ceiling-test-model", "low", self.profiles)
        self.assertEqual(res_low, {
            "requested_effort": "low",
            "effective_effort": "high",
            "status": "mapped",
            "source": "ladder_ceiling",
        })

        # "medium" requested -> ceiling is "high"
        res_med = resolve_model_reasoning_effort("ceiling-test-model", "medium", self.profiles)
        self.assertEqual(res_med, {
            "requested_effort": "medium",
            "effective_effort": "high",
            "status": "mapped",
            "source": "ladder_ceiling",
        })

        # "xhigh" requested -> ceiling is "max"
        res_xh = resolve_model_reasoning_effort("ceiling-test-model", "xhigh", self.profiles)
        self.assertEqual(res_xh, {
            "requested_effort": "xhigh",
            "effective_effort": "max",
            "status": "mapped",
            "source": "ladder_ceiling",
        })

        # If a model only supports [minimal, low], requesting "high" has no ceiling, falls back to max ("low")
        partial_profiles = {
            "capped-model": {"supported_efforts": ["minimal", "low"]}
        }
        res_high = resolve_model_reasoning_effort("capped-model", "high", partial_profiles)
        self.assertEqual(res_high, {
            "requested_effort": "high",
            "effective_effort": "low",
            "status": "mapped",
            "source": "ladder_max",
        })

    def test_unsupported_model(self):
        res = resolve_model_reasoning_effort("unsupported-model", "high", self.profiles)
        self.assertEqual(res, {
            "requested_effort": "high",
            "effective_effort": None,
            "status": "unsupported",
            "source": "unsupported_profile",
        })
        res_none = resolve_model_reasoning_effort("unsupported-model", None, self.profiles)
        self.assertEqual(res_none, {
            "requested_effort": None,
            "effective_effort": None,
            "status": "unsupported",
            "source": "unsupported_profile",
        })

    def test_invalid_model_arg(self):
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("", "high", self.profiles)
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("   ", "high", self.profiles)
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort(123, "high", self.profiles)

    def test_invalid_requested_effort_raises(self):
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("antigravity/gemini", "typo", self.profiles)
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("antigravity/gemini", True, self.profiles)
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("antigravity/gemini", ["high"], self.profiles)
        # Even for unknown models or unsupported models, invalid requested effort must ValueError
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("unknown-model", "bad_effort", self.profiles)
        with self.assertRaises(ValueError):
            resolve_model_reasoning_effort("unsupported-model", "bad_effort", self.profiles)

    def test_unknown_model_preserves_legacy(self):
        res = resolve_model_reasoning_effort("unknown-model", "xhigh", self.profiles)
        self.assertEqual(res, {
            "requested_effort": "xhigh",
            "effective_effort": "xhigh",
            "status": "unknown",
            "source": "legacy_unknown",
        })

        # profiles is None
        res2 = resolve_model_reasoning_effort("any-model", "medium", None)
        self.assertEqual(res2, {
            "requested_effort": "medium",
            "effective_effort": "medium",
            "status": "unknown",
            "source": "legacy_unknown",
        })


if __name__ == "__main__":
    unittest.main()
