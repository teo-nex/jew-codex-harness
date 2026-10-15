"""Optional routing tiers and real Codex client-version propagation."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, NATIVE_MODELS, REQUIRED_NATIVE_MODELS, validate_config
from routing_policy import (ASTRA, MODEL_IDS, QUESTIONS, SOL61, decision_from_answers,
                            questions_for_models, route_choice)


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

    def test_choice_distribution_is_validated_against_the_offered_set(self):
        original = copy.deepcopy(QUESTIONS)
        for models in (REQUIRED_NATIVE_MODELS, NATIVE_MODELS):
            questions = questions_for_models(models)
            choices = questions['model']['criteria']
            self.assertEqual({MODEL_IDS[key] for key in choices}, set(models))
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
