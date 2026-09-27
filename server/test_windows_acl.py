"""Windows must decide credential privacy from the DACL, not st_mode."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jev_server
import local_runtime


@unittest.skipUnless(os.name == "nt", "Windows DACL test")
class WindowsCredentialAcl(unittest.TestCase):
    def _set_acl(self, path: Path, shared: bool) -> None:
        script = (
            "$ErrorActionPreference='Stop'; "
            "$p=$env:JEV_TEST_KEY_PATH; "
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
            "$acl=[System.Security.AccessControl.FileSecurity]::new(); "
            "$acl.SetAccessRuleProtection($true,$false); "
            "$allow=[System.Security.AccessControl.AccessControlType]::Allow; "
            "$full=[System.Security.AccessControl.FileSystemRights]::FullControl; "
            "$none=[System.Security.AccessControl.InheritanceFlags]::None; "
            "$prop=[System.Security.AccessControl.PropagationFlags]::None; "
            "$acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($sid,$full,$none,$prop,$allow)); "
            "if ($env:JEV_TEST_SHARED -eq '1') { "
            "$users=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'); "
            "$read=[System.Security.AccessControl.FileSystemRights]::Read; "
            "$acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($users,$read,$none,$prop,$allow)) }; "
            "[System.IO.File]::SetAccessControl($p,$acl)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "JEV_TEST_KEY_PATH": str(path),
                 "JEV_TEST_SHARED": "1" if shared else "0"},
            check=True, capture_output=True, timeout=45,
        )

    def test_secret_loaders_reject_shared_acl(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "fixture.key"
            path.write_text("synthetic-key\n", encoding="utf-8")
            self._set_acl(path, shared=False)
            self.assertTrue(local_runtime.private_regular(path.stat(), path))
            with mock.patch.object(local_runtime, "AUTH_PATH", str(path)):
                self.assertEqual(local_runtime.local_secret(), "synthetic-key")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(path)}, clear=True):
                self.assertEqual(jev_server.load_key(), "synthetic-key")

            self._set_acl(path, shared=True)
            self.assertFalse(local_runtime.private_regular(path.stat(), path))
            with mock.patch.object(local_runtime, "AUTH_PATH", str(path)):
                self.assertEqual(local_runtime.local_secret(), "")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(path)}, clear=True):
                with self.assertRaises(PermissionError):
                    jev_server.load_key()


if __name__ == "__main__":
    unittest.main()
