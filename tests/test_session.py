import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from harness import cli, core, session


@unittest.skipUnless(shutil.which("node"), "Node required for session CLI")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jev-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = Path(__file__).resolve().parents[1]
        self.home = self.root / "profile with space and 'quote"
        self.state = self.home / "codex-router"
        self.state.mkdir(parents=True)
        directory = self.home / "jev-harness"
        directory.mkdir()
        manifest = {"version": 1, "codex_home": str(self.home), "repo": str(self.repo),
                    "service_id": "synthetic-service"}
        data = json.dumps(manifest).encode()
        (directory / "manifest.json").write_bytes(data)
        (directory / "journal.json").write_text(json.dumps({**manifest, "phase": "complete",
            "manifest_sha256": hashlib.sha256(data).hexdigest()}))
        self.auth = {"auth_mode": "chatgpt", "tokens": {
            "access_token": "synthetic-session-secret", "account_id": "synthetic-account"}}
        (self.home / "auth.json").write_text(json.dumps(self.auth))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MODEL_ROUTER_", "CODEX_", "KIMI_", "JEV_"))}
        env["HOME"] = str(self.root)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def test_status_does_not_write_or_enable_consent(self):
        before = sorted(p.relative_to(self.home) for p in self.home.rglob("*"))
        result = session.manage(self.repo, self.home, "status")
        self.assertEqual(result["codex_home"], str(self.home))
        self.assertEqual(result["state_dir"], str(self.state))
        self.assertEqual(result["sharing"], "disabled")
        self.assertEqual(result["session"], "usable")
        self.assertEqual(before, sorted(p.relative_to(self.home) for p in self.home.rglob("*")))
        self.assertNotIn("synthetic-session-secret", json.dumps(result))
        self.assertNotIn("synthetic-account", json.dumps(result))

    def test_enable_requires_explicit_owner_opt_in(self):
        with self.assertRaisesRegex(core.InstallError, "owner approval"):
            session.manage(self.repo, self.home, "enable")
        self.assertFalse((self.state / "discovery-mode.json").exists())
        self.assertFalse((self.state / "native-session-consent.json").exists())

    def test_stale_parent_state_and_auth_cannot_redirect_selected_profile(self):
        other = self.root / "other"
        other.mkdir()
        marker = other / "native-session-consent.json"
        marker.write_text('{"version":1,"sharing":"enabled"}')
        stale = {"MODEL_ROUTER_STATE_DIR": str(other), "CODEX_ROUTER_STATE_DIR": str(other),
                 "KIMI_CODEX_STATE_DIR": str(other), "MODEL_ROUTER_CODEX_AUTH": str(other / "absent.json")}
        with mock.patch.dict(os.environ, stale):
            self.assertEqual(session.manage(self.repo, self.home, "status")["sharing"], "disabled")
            enabled = session.manage(self.repo, self.home, "enable", allow_sharing=True)
            self.assertEqual(enabled["sharing"], "enabled")
            self.assertEqual(enabled["session"], "usable")
            self.assertEqual(enabled["state_dir"], str(self.state))
            self.assertTrue((self.state / "native-session-consent.json").exists())
            self.assertEqual(os.environ["MODEL_ROUTER_STATE_DIR"], str(other))
            disabled = session.manage(self.repo, self.home, "disable")
            self.assertEqual(disabled["sharing"], "disabled")
        self.assertEqual(list(other.iterdir()), [marker])
        self.assertEqual(marker.read_text(), '{"version":1,"sharing":"enabled"}')

    def test_forced_discovery_off_does_not_mutate_state(self):
        with mock.patch.dict(os.environ, {"CODEX_ROUTER_NO_DISCOVERY": "1"}):
            with self.assertRaisesRegex(core.InstallError, "NO_DISCOVERY"):
                session.manage(self.repo, self.home, "enable", allow_sharing=True)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_forced_sharing_off_does_not_enable_discovery(self):
        with mock.patch.dict(os.environ, {"CODEX_ROUTER_NATIVE_SESSION_FALLBACK": "0"}):
            with self.assertRaisesRegex(core.InstallError, "NATIVE_SESSION_FALLBACK"):
                session.manage(self.repo, self.home, "enable", allow_sharing=True)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_missing_login_never_creates_sharing_consent(self):
        (self.home / "auth.json").unlink()
        with self.assertRaisesRegex(core.InstallError, "No usable ChatGPT login"):
            session.manage(self.repo, self.home, "enable", allow_sharing=True)
        self.assertFalse((self.state / "native-session-consent.json").exists())

    def test_changed_manifest_is_rejected_before_child_execution(self):
        manifest = self.home / "jev-harness/manifest.json"
        manifest.write_text(manifest.read_text() + "\n")
        with mock.patch.object(session, "run_bounded") as run:
            with self.assertRaisesRegex(core.InstallError, "ownership mismatch"):
                session.manage(self.repo, self.home, "status")
            run.assert_not_called()

    def test_cli_dispatch_uses_explicit_profile_and_never_exposes_child_errors(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--codex-home", str(self.home), "session", "status"], repo=self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["state_dir"], str(self.state))
        error = io.StringIO()
        with mock.patch.object(session, "run_bounded", return_value=mock.Mock(
                returncode=1, stderr="synthetic-private-secret", stdout="")), redirect_stderr(error):
            code = cli.main(["--codex-home", str(self.home), "session", "status"], repo=self.repo)
        self.assertEqual(code, 2)
        self.assertNotIn("synthetic-private-secret", error.getvalue())

    def test_unknown_env_override_value_is_not_echoed(self):
        with mock.patch.dict(os.environ, {"CODEX_ROUTER_NO_DISCOVERY": "synthetic-secret"}):
            status = session.manage(self.repo, self.home, "status")
        self.assertEqual(status["discovery_override"], "unrecognized")
        self.assertNotIn("synthetic-secret", json.dumps(status))
