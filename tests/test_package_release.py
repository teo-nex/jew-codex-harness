from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.package_release import ReleaseError, build, member_policy, scan_private_metadata, scan_secret


class PackageReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Release Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "release@example.invalid"], check=True)
        files = {
            ".gitignore": "artifacts/\n",
            "README.md": "portable harness\n",
            "ROUTER_FORK.md": "pinned source snapshot\n",
            "AGENTS.md": "install contract\n",
            "LICENSE": "test license\n",
            "install.sh": "#!/bin/sh\n",
            "install.ps1": "Write-Output ok\n",
            "harness/cli.py": "print('cli')\n",
            "harness/onboard.py": "print('onboard')\n",
            "server/jev_fetch.mjs": "export const fetchJev = true;\n",
            "integrations/jev/global/codex_hook.py": "print('hook')\n",
            "router/LICENSE": "router license\n",
            "router/src/jev-fallback.mjs": "export const pinned = true;\n",
            "tests/test_core.py": "def test_core(): pass\n",
            "docs/INSTALL.md": "manual\n",
            "docs/RELEASING.md": "release checklist\n",
        }
        for name, value in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_build_is_reproducible_and_manifest_hashes_match(self) -> None:
        first = self.root / "first.tar.gz"
        second = self.root / "second.tar.gz"
        result1 = build(self.repo, first)
        result2 = build(self.repo, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result1["artifact_sha256"], result2["artifact_sha256"])
        with __import__("tarfile").open(first, "r:gz") as archive:
            manifest = json.load(archive.extractfile("RELEASE-MANIFEST.json"))
            for item in manifest["files"]:
                payload = archive.extractfile(item["path"]).read()
                self.assertEqual(len(payload), item["size"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
            self.assertIn("router/src/jev-fallback.mjs", {f["path"] for f in manifest["files"]})
        self.assertEqual(result1["artifact_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())

    def test_runtime_venv_and_generated_state_are_never_archived(self) -> None:
        for name in (".runtime/auth.json", ".venv/secret.txt", "node_modules/cache.js", "logs/run.log"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("local state", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-f", ".runtime/auth.json", ".venv/secret.txt", "node_modules/cache.js", "logs/run.log"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "tracked local state fixture"], check=True)
        artifact = self.root / "release.tar.gz"
        result = build(self.repo, artifact)
        with __import__("tarfile").open(artifact, "r:gz") as archive:
            names = archive.getnames()
        for forbidden in (".runtime/auth.json", ".venv/secret.txt", "node_modules/cache.js", "logs/run.log"):
            self.assertNotIn(forbidden, names)
        self.assertEqual(result["files"], 16)

    def test_secret_like_tracked_filename_fails_closed(self) -> None:
        path = self.repo / "config/provider-secret.json"
        path.parent.mkdir()
        path.write_text('{"token":"fixture"}', encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "bad secret path"], check=True)
        with self.assertRaisesRegex(ReleaseError, "credential-like tracked path"):
            build(self.repo, self.root / "bad.tar.gz")

    def test_source_code_named_auth_is_releasable(self) -> None:
        from scripts.package_release import member_policy
        self.assertEqual(member_policy("server/configure-auth.mjs"), "include")
        self.assertEqual(member_policy("router/src/provider-credentials.mjs"), "include")

    def test_private_aggregate_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "operator aggregate"):
            member_policy("poc/backtest-sample-results.json")

    def test_first_party_private_paths_and_email_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "private home path"):
            scan_private_metadata(b"/Users/" + b"privateperson/Documents/work", "docs/guide.md")
        with self.assertRaisesRegex(ReleaseError, "private email"):
            scan_private_metadata(b"contact@" + b"personal-domain.test", "docs/guide.md")
        scan_private_metadata(b"/home/" + b"example/sessions", "server/test_runtime_paths.py")

    def test_known_synthetic_key_is_only_allowed_in_its_fixture_file(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "router/scripts/verify-grok-service-tier.mjs"
        import re
        match = re.search(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b", fixture.read_bytes())
        self.assertIsNotNone(match)
        scan_secret(match.group(), "router/scripts/verify-grok-service-tier.mjs")
        with self.assertRaisesRegex(ReleaseError, "possible credential material"):
            scan_secret(match.group(), "config/provider.json")

    def test_secret_shaped_content_fails_closed(self) -> None:
        path = self.repo / "docs/token-example.txt"
        path.write_text("bot=" + "1234567890:" + "A" * 36 + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "secret content"], check=True)
        with self.assertRaisesRegex(ReleaseError, "possible credential material"):
            build(self.repo, self.root / "bad.tar.gz")

    def test_typesafe_shaped_key_is_rejected(self) -> None:
        candidate = b"apikey_" + b"a" * 64
        with self.assertRaisesRegex(ReleaseError, "possible credential material"):
            scan_secret(candidate, "config/example.txt")

    def test_dirty_tree_is_rejected(self) -> None:
        (self.repo / "README.md").write_text("uncommitted\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "clean Git tree"):
            build(self.repo, self.root / "bad.tar.gz")

    def test_output_must_be_outside_repo_or_gitignored(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "inside the repository"):
            build(self.repo, self.repo / "release.tar.gz")
        ignored = self.repo / "artifacts" / "release.tar.gz"
        result = build(self.repo, ignored)
        self.assertTrue(Path(result["sha256_file"]).exists())

    def test_unsafe_paths_are_rejected(self) -> None:
        for name in ("../escape", "/absolute", "folder/../escape", "a\\b"):
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                member_policy(name)

    def test_size_limit_is_enforced(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "source exceeds configured size limit"):
            build(self.repo, self.root / "large.tar.gz", max_bytes=8)

    def test_artifact_symlink_is_rejected(self) -> None:
        target = self.root / "must-not-change"
        target.write_text("leave alone\n", encoding="utf-8")
        link = self.root / "release.tar.gz"
        link.symlink_to(target)
        with self.assertRaisesRegex(ReleaseError, "artifact symlink"):
            build(self.repo, link)
        self.assertEqual(target.read_text(encoding="utf-8"), "leave alone\n")


if __name__ == "__main__":
    unittest.main()
