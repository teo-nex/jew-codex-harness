"""Offline release regressions for route dispatch and bounded continuity."""
import os
import unittest
from unittest import mock

import jev_server as j
from provider_ladder import Ladder


class ReleaseRoutingTests(unittest.TestCase):
    def setUp(self):
        j._route_leases.clear()
        j._cache_affinity.clear()
        self.addCleanup(j._route_leases.clear)
        self.addCleanup(j._cache_affinity.clear)
        self.payload = {"prompt_cache_key": "fixture", "input": "Inspect the fixture"}
        self.step = {"step_type": "tool_step", "tool_call": {"name": "read"}}
        self.decision = {"model": "astra", "effort": "high", "lease": "user_turn"}

    def test_repeated_success_does_not_renew_a_lease_forever(self):
        j.remember_route_lease("scope", self.payload, self.step, self.decision, 200, now=100)
        for index in range(8):
            now = 101 + index
            self.assertIsNotNone(j.route_lease("scope", self.payload, self.step, now=now))
            j.remember_route_lease("scope", self.payload, self.step, self.decision, 200, now=now)
        self.assertIsNone(j.route_lease("scope", self.payload, self.step, now=110))

    def test_absolute_age_expires_even_with_recent_success(self):
        j.remember_route_lease("scope", self.payload, self.step, self.decision, 200, now=100)
        self.assertIsNotNone(j.route_lease("scope", self.payload, self.step, now=350))
        j.remember_route_lease("scope", self.payload, self.step, self.decision, 200, now=350)
        self.assertIsNone(j.route_lease("scope", self.payload, self.step, now=401))

    def test_abandoned_scopes_are_bounded_and_collected(self):
        with mock.patch.object(j, "ROUTE_MAX_SCOPES", 4):
            for index in range(10):
                j.remember_route_lease(str(index), self.payload, self.step, self.decision, 200, now=100 + index)
                j.remember_cache_model(str(index), self.payload, j.SOL, 200, now=100 + index)
            self.assertEqual(len(j._route_leases), 4)
            self.assertEqual(len(j._cache_affinity), 4)
            self.assertNotIn("0", j._route_leases)
            self.assertNotIn("0", j._cache_affinity)
            self.assertIsNone(j.route_lease("absent", self.payload, self.step, now=2000))
            self.assertIsNone(j.cache_affinity("absent", self.payload, now=2000))
            self.assertFalse(j._route_leases)
            self.assertFalse(j._cache_affinity)

    def test_active_ladder_is_used_without_a_prompt_cache_key(self):
        class LadderReached(Exception):
            pass
        ladder = mock.Mock(spec=Ladder)
        ladder.config = {"provider_sequence": [{"model": "fixture/provider"}], "request_budget": {}}
        for key in (None, "", "fixture-key"):
            with self.subTest(cache_key=key):
                handler = object.__new__(j.Handler)
                handler.path = "/v1/responses"
                handler._body = mock.Mock(return_value={"input": "Fixture", "prompt_cache_key": key})
                handler._admit_replay = mock.Mock(return_value=True)
                handler._serve_ladder = mock.Mock(side_effect=LadderReached)
                with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                     mock.patch.object(j, "_provider_ladder", return_value=ladder), \
                     mock.patch.object(j, "resolve_project_policy", return_value=None), \
                     mock.patch.object(j, "load_key", return_value=None), \
                     mock.patch.object(j.os.path, "exists", return_value=False):
                    with self.assertRaises(LadderReached):
                        handler._post()
                handler._serve_ladder.assert_called_once()
