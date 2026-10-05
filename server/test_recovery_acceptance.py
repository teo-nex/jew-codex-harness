import unittest
from unittest import mock

import recovery_acceptance as recovery
import jev_server as jev


class RecoveryAcceptanceTests(unittest.TestCase):
    def test_fixture_stream_uses_valid_actual_transport_contract(self):
        raw = recovery.response_stream()
        self.assertIsNone(jev.external_sse_issue(raw))
        assembled = jev.assemble_sse(raw)
        self.assertEqual(assembled["model"], "fixture/model-b")
        self.assertEqual(assembled["status"], "completed")
        self.assertEqual(assembled["output"][0]["content"][0]["text"], recovery.MARKER)

    def test_no_client_does_not_start_servers(self):
        with mock.patch.object(recovery.shutil, "which", return_value=None), \
             mock.patch.object(recovery, "ThreadingHTTPServer") as server:
            self.assertEqual(recovery.run()["status"], "blocked")
        server.assert_not_called()
