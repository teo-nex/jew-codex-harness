import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, validate_config
from project_policy import resolve, restrict, final_required


CONFIG = {"providers": [{"id": "paid", "model": "vendor/paid", "billing": "paid"},
                         {"id": "free", "model": "vendor/free", "billing": "free"},
                         {"id": "unknown", "model": "vendor/unknown"},
                         {"id": "native", "transport": "native"}],
          "project_policies": {"/project": {"no_paid_fallback": True, "allowed_providers": ["free"],
                                              "require_astra_final": True}},
          "project_scopes": {"0123456789abcdef": "/project"}}


class ProjectPolicyTests(unittest.TestCase):
    def test_payment_and_allowlist_filter_and_unbound_fail_closed(self):
        config = validate_config(CONFIG)
        policy = resolve(config, "0123456789abcdef")
        self.assertEqual([p["id"] for p in restrict(config, policy)["providers"]], ["free", "native"])
        self.assertEqual([p["id"] for p in restrict(config, resolve(config, "unbound"))["providers"]], ["native"])
        no_paid = restrict(config, {"no_paid_fallback": True})
        self.assertEqual([p["id"] for p in no_paid["providers"]], ["free", "native"])

    def test_final_review_has_exact_astra_or_fails_not_downgrades(self):
        config = validate_config(CONFIG)
        policy = resolve(config, "0123456789abcdef")
        self.assertTrue(final_required(policy, {"metadata": {"jev_phase": "final_review"}}, None))
        self.assertTrue(final_required(policy, {}, {"astra_policy": "astra"}))
        restricted = restrict(config, policy, True)
        self.assertEqual([p["id"] for p in restricted["providers"]], ["native"])
        restricted["providers"][0]["model"] = "gpt-6-sol"
        with self.assertRaises(ValueError):
            restrict(restricted, policy, True)

    def test_bad_policy_and_binding_rejected(self):
        for change in ({"project_scopes": {"bad": "/project"}},
                       {"project_policies": {"relative": {}}},
                       {"project_policies": {"/project": {"native_only": "true"}}},
                       {"project_policies": {"/project": {"allowed_providers": [{}]}}}):
            with self.assertRaises(ValueError):
                validate_config({**CONFIG, **change})

    def test_native_only_skips_external_jev_even_off_or_shadow(self):
        payload = {"model": "jev/auto", "input": "private task", "prompt_cache_key": "session"}
        scope = jev.cache_scope(payload, "private task")
        config = copy.deepcopy(CONFIG)
        config["project_scopes"] = {scope: "/project"}
        config["project_policies"]["/project"] = {"native_only": True}
        handler = object.__new__(jev.Handler)
        handler.path = "/v1/responses"
        handler._body = lambda _: payload
        handler._serve_ladder = mock.Mock()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(jev.os.environ, {"JEV_LADDER_MODE": "active"}), \
             mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, Path(root) / "state.json")), \
             mock.patch.object(jev, "call_jev_routed") as decision:
            handler._post()
        decision.assert_not_called()
        self.assertEqual(handler._serve_ladder.call_args.args[2], "gpt-6-astra")
