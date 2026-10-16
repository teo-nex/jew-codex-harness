"""Optional routing tiers and real Codex client-version propagation."""
import copy
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, NATIVE_MODELS, REQUIRED_NATIVE_MODELS, validate_config
from routing_policy import (ASTRA, MODEL_IDS, QUESTIONS, SOL61, decision_from_answers,
                            automatic_models, workhorse, questions_for_models, route_choice)


class OptionalModelRouting(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state.json'

    def test_existing_maps_remain_valid_and_do_not_offer_unmapped_choices(self):
        config = {'providers': [{'id': 'gateway', 'models': {
            model: 'gateway/' + model for model in REQUIRED_NATIVE_MODELS}}]}
        ladder = Ladder(config, self.state)
        self.assertEqual(ladder.available_models(), REQUIRED_NATIVE_MODELS)
        for model in REQUIRED_NATIVE_MODELS:
            self.assertEqual(ladder.route('scope', model, 'high')['model'], 'gateway/' + model)
        with self.assertRaises(ValueError):
            ladder.route('scope', SOL61, 'high')

    def test_optional_choice_preserves_exact_model_and_requires_fallback_coverage(self):
        config = {'providers': [{'id': 'gateway', 'models': {
            model: 'gateway/' + model for model in NATIVE_MODELS}}]}
        ladder = Ladder(config, self.state)
        self.assertIn(SOL61, ladder.available_models())
        self.assertEqual(ladder.route('scope', SOL61, 'max')['model'], 'gateway/' + SOL61)
        config['providers'].append({'id': 'fallback', 'models': {
            model: 'fallback/' + model for model in REQUIRED_NATIVE_MODELS}})
        self.assertNotIn(SOL61, Ladder(config, self.state).available_models())
        config['providers'][1] = {'id': 'fallback', 'transport': 'native'}
        self.assertIn(SOL61, Ladder(config, self.state).available_models())
        config['providers'][0]['models']['unknown-model'] = 'gateway/unknown'
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_auto_replaces_legacy_sol_but_explicit_routes_remain_valid(self):
        self.assertNotIn("gpt-6-sol", automatic_models(NATIVE_MODELS))
        self.assertIn("gpt-6-sol", automatic_models(REQUIRED_NATIVE_MODELS))
        self.assertEqual(workhorse(NATIVE_MODELS), SOL61)
        self.assertEqual(workhorse(REQUIRED_NATIVE_MODELS), "gpt-6-sol")
        self.assertNotEqual(workhorse(("gpt-6-luna", ASTRA)), ASTRA)
        with self.assertRaises(ValueError):
            workhorse((ASTRA,))

    def test_ladder_receives_bounded_failure_reason_instead_of_frontier_escalation(self):
        for models, expected in ((NATIVE_MODELS, SOL61), (REQUIRED_NATIVE_MODELS, "gpt-6-sol")):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in models}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": "jev/auto", "input": "read fixture", "prompt_cache_key": "fixture-bounded"}
            handler._serve_ladder = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "load_key", return_value="fixture-key"), \
                 mock.patch.object(jev, "route_lease", return_value=None), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed", side_effect=TimeoutError("private fixture")):
                handler._post()
            self.assertEqual(handler._serve_ladder.call_args.args[2:4], (expected, "medium"))
            self.assertEqual(handler._serve_ladder.call_args.kwargs["routing_gate"], "jev_error:TimeoutError")

    def test_explicit_models_use_the_ladder_without_classification_or_a_cache_key(self):
        for requested in (SOL61, "gpt-6-sol", ASTRA):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in NATIVE_MODELS}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": requested, "input": "bounded fixture", "reasoning": {"effort": "low"}}
            handler._serve_ladder = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "route_lease", return_value={"model": "gpt-6-luna"}), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed") as classify:
                handler._post()
            classify.assert_not_called()
            self.assertEqual(handler._serve_ladder.call_args.args[2:4], (requested, "low"))
            self.assertEqual(handler._serve_ladder.call_args.args[-1], "manual")
            self.assertEqual(handler._serve_ladder.call_args.kwargs["routing_gate"], "manual")

    def test_unmapped_fixed_models_and_invalid_effort_fail_without_substitution(self):
        for requested, effort in ((SOL61, "low"), ("gpt-6-sol", "invalid")):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in REQUIRED_NATIVE_MODELS}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": requested, "input": "fixture", "reasoning": {"effort": effort}}
            handler._serve_ladder = mock.Mock()
            handler._json = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "route_lease", return_value=None), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed") as classify:
                handler._post()
            self.assertEqual(handler._json.call_args.args[0], 400)
            handler._serve_ladder.assert_not_called()
            classify.assert_not_called()

    def test_choice_distribution_is_validated_against_the_offered_set(self):
        original = copy.deepcopy(QUESTIONS)
        for models in (REQUIRED_NATIVE_MODELS, NATIVE_MODELS):
            questions = questions_for_models(models)
            choices = questions['model']['criteria']
            self.assertEqual({MODEL_IDS[key] for key in choices}, set(automatic_models(models)))
            selected = SOL61 if SOL61 in models else REQUIRED_NATIVE_MODELS[0]
            pairs = route_choice(selected, 'high')
            answer = {key: {'choice': value, 'confidence': 0.99} for key, value in pairs.items()}
            answer['model']['probabilities'] = {key: float(key == pairs['model']) for key in choices}
            self.assertEqual(decision_from_answers(answer, models=models)['model'], selected)
            answer['astra_policy']['choice'] = 'astra'
            self.assertEqual(decision_from_answers(answer, models=models)['model'], ASTRA)
        self.assertNotIn('sol61', questions_for_models(REQUIRED_NATIVE_MODELS)['model']['instructions'])
        with self.assertRaises(ValueError):
            decision_from_answers({key: {'choice': value} for key, value in
                                   route_choice(SOL61, 'high').items()}, models=REQUIRED_NATIVE_MODELS)
        self.assertEqual(QUESTIONS, original)


class ClientVersionPropagation(unittest.TestCase):
    def test_actual_client_headers_take_precedence_over_local_detection(self):
        with mock.patch.object(jev, 'installed_codex_version', return_value='1.2.3') as detect:
            for headers in ({'Version': '2.3.4'}, {'version': '2.3.4'},
                            {'User-Agent': 'codex_cli_rs/2.3.4 (fixture)'},
                            {'User-Agent': 'codex-cli/2.3.4'},
                            {'user-agent': 'codex_exec/2.3.4 (fixture)'}):
                self.assertEqual(jev.codex_client_version(headers), '2.3.4')
            detect.assert_not_called()
            for headers in ({}, {'Version': 'bad\r\nHeader: value'},
                            {'User-Agent': 'other-client/9.9.9'},
                            {'User-Agent': 'codex-router/9.8.7'},
                            {'User-Agent': 'codex-proxy/9.8.7'},
                            {'User-Agent': 'not-codex_exec/9.8.7'}):
                self.assertEqual(jev.codex_client_version(headers), '1.2.3')

    def test_installed_version_is_detected_once_and_never_guessed_on_failure(self):
        for output, expected in [('codex-cli 1.2.3\n', '1.2.3'), ('not a version', None)]:
            with self.subTest(output=output), \
                 mock.patch.object(jev, '_client_version_cache', {}), \
                 mock.patch.object(jev, '_node_binary', return_value='/fixture/node'), \
                 mock.patch.object(jev.os.path, 'isfile', return_value=True), \
                 mock.patch.object(jev.subprocess, 'run', return_value=
                                   subprocess.CompletedProcess([], 0, stdout=output, stderr='')) as run:
                self.assertEqual(jev.installed_codex_version(), expected)
                self.assertEqual(jev.installed_codex_version(), expected)
                self.assertEqual(run.call_count, 1)
                self.assertIn('codex_client_version.mjs', run.call_args.args[0][-1])
                self.assertNotIn('shell', run.call_args.kwargs)

    @unittest.skipIf(os.name == "nt", "router PATH shims are POSIX-only")
    def test_version_detection_walks_past_router_shims_and_preserves_overrides(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        helper = Path(__file__).with_name("codex_client_version.mjs")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def executable(folder, version, shim=False):
                file = root / folder / "codex"
                file.parent.mkdir()
                file.write_text("#!/bin/sh\n" + ("# MODEL_ROUTER_CODEX_SHIM\nexit 99\n" if shim else
                                'printf "codex-cli ' + version + '\\n"\n'))
                file.chmod(0o700)
                return file
            shim = executable("shim", "", True)
            second_shim = executable("second-shim", "", True)
            real = executable("real", "7.8.9")
            later = executable("later", "8.9.0")
            explicit = executable("explicit", "9.0.1")
            env = dict(os.environ)
            env.pop("CODEX_BIN", None); env.pop("CODEX_INSTALL_DIR", None)
            env["PATH"] = os.pathsep.join([str(p.parent) for p in (shim, second_shim, real, later)]
                                         + [env.get("PATH", "")])
            for overrides, expected in (({}, "7.8.9"), ({"CODEX_BIN": str(explicit)}, "9.0.1"),
                                        ({"CODEX_INSTALL_DIR": str(explicit.parent)}, "9.0.1"),
                                        ({"CODEX_BIN": str(shim)}, "7.8.9")):
                with self.subTest(overrides=overrides):
                    result = subprocess.run([node, str(helper)], env={**env, **overrides},
                                            capture_output=True, text=True, timeout=15, check=True)
                    self.assertEqual(result.stdout.strip(), "codex-cli " + expected)

    @unittest.skipUnless(sys.platform == "darwin", "known macOS service CLI paths")
    def test_service_path_prefers_known_terminal_cli_over_older_desktop_bundle(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        with tempfile.TemporaryDirectory() as directory:
            preload = Path(directory) / "resolver-fixture.mjs"
            preload.write_text("""
import fs from 'node:fs';
import cp from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const originalExists=fs.existsSync;
const selected=process.env.TEST_TERMINAL_CLI;
const desktop='/Applications/Codex.app/Contents/Resources/codex';
fs.existsSync=(file)=>String(file).endsWith('/codex')
  ? file===selected || file===desktop : originalExists(file);
cp.execFileSync=(file)=>{
  if(file===selected)return 'codex-cli 7.8.9';
  if(file===desktop)return 'codex-cli 1.2.3';
  throw new Error('No fixture command on PATH');
};
syncBuiltinESMExports();
""")
            env = dict(os.environ)
            env.pop("CODEX_BIN", None); env.pop("CODEX_INSTALL_DIR", None)
            env["PATH"] = directory
            for selected in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
                with self.subTest(selected=selected):
                    result = subprocess.run([node, "--import", str(preload),
                                            str(Path(__file__).with_name("codex_client_version.mjs"))],
                                           env={**env, "TEST_TERMINAL_CLI": selected},
                                           capture_output=True, text=True, timeout=15, check=True)
                    self.assertEqual(result.stdout.strip(), "codex-cli 7.8.9")

    def test_external_forward_sends_detected_version_without_changing_payload(self):
        sent = []
        class Response:
            status = 200
            def getheader(self, name):
                return 'application/json' if name == 'Content-Type' else None
            def read(self, *args):
                return b'{"id":"r","status":"completed","output":[]}'
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, body, headers): sent.append((json.loads(body), headers))
            def getresponse(self): return Response()
            def close(self): pass
        handler = object.__new__(jev.Handler)
        handler._attempts = []; handler._response_started = False
        handler.wfile = io.BytesIO(); handler.headers = {'User-Agent': 'codex-router/9.8.7'}
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        payload = {'model': 'gateway/model', 'input': 'fixture'}
        with mock.patch.object(jev.http.client, 'HTTPConnection', Connection), \
             mock.patch.object(jev, 'installed_codex_version', return_value='1.2.3'):
            status, *_ = handler._forward(payload, '/v1/responses', False, False, '',
                                         'gateway/model', external={'host': 'localhost',
                                                                  'port': 1, 'key': 'synthetic'})
        self.assertEqual(status, 200)
        self.assertEqual(sent[0][0], payload)
        self.assertEqual(sent[0][1]['Version'], '1.2.3')


if __name__ == '__main__':
    unittest.main()
