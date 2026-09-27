#!/usr/bin/env python3
"""Build a deterministic, secret-screened source release from a clean Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from gzip import GzipFile
from pathlib import Path, PurePosixPath

VERSION = "1.0"
DEFAULT_MAX_SOURCE_BYTES = 100 * 1024 * 1024
OMIT_DIRS = {
    ".git", ".runtime", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    "artifacts", "dist", "build", "coverage", ".codex", ".hermes",
}
OMIT_SUFFIXES = {".pyc", ".pyo", ".log", ".jsonl", ".sqlite", ".sqlite3", ".db"}
SOURCE_SUFFIXES = {".py", ".mjs", ".js", ".ts", ".tsx", ".sh", ".ps1", ".md"}
SECRET_NAME = re.compile(
    r"(^|[._-])(auth|credential|credentials|secret|secrets|password|passwd)([._-]|$)",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(rb"\bapikey_[0-9a-f]{40,}\b"),  # TypeSafe-style API key
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),  # common API key shape
    re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),  # Anthropic key
    re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ID
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub token
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
FIRST_PARTY_HOME = re.compile(rb"/(?:Users|home)/([A-Za-z0-9._-]+)|[A-Za-z]:\\Users\\([A-Za-z0-9._-]+)")
FIRST_PARTY_EMAIL = re.compile(rb"(?i)[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})")
SYNTHETIC_HOME_SEGMENTS = {"example", "x"}
KNOWN_SYNTHETIC_KEY_HASHES = {
    "router/scripts/verify-grok-service-tier.mjs": {
        "76291638580a809294c0ffbfa8dd71b20c87a8665a3e4f97fede0403a60fac59",
    },
    "router/test/routing.test.mjs": {
        "cf3abe0a1cd75cb90ed0bc9f25725044f8bfd8013c3cb59cc1bdeb925c37d7ef",
    },
}


class ReleaseError(RuntimeError):
    pass


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=False,
    )
    if check and proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise ReleaseError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def validate_clean_tree(repo: Path) -> str:
    if not (repo / ".git").exists() and run_git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise ReleaseError(f"not a Git work tree: {repo}")
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ReleaseError("release requires a clean Git tree (tracked and untracked changes found)")
    commit = run_git(repo, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ReleaseError("Git returned an invalid commit ID")
    return commit


def member_policy(name: str) -> str:
    """Return include/omit, rejecting suspicious paths rather than normalizing them."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise ReleaseError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError(f"unsafe archive member path: {name!r}")
    if str(path) != name:
        raise ReleaseError(f"non-canonical archive member path: {name!r}")
    if name == "poc/backtest-sample-results.json":
        raise ReleaseError("operator aggregate sample is not releasable")
    if any(part in OMIT_DIRS for part in path.parts) or path.suffix.lower() in OMIT_SUFFIXES:
        return "omit"
    if path.name in {".DS_Store", "Thumbs.db"}:
        return "omit"
    if any(part.lower() in {"credentials", "secrets"} for part in path.parts[:-1]):
        raise ReleaseError(f"credential-like tracked path is not releasable: {name}")
    if ((SECRET_NAME.search(path.name) and path.suffix.lower() not in SOURCE_SUFFIXES)
            or path.name.lower().startswith(".env")
            or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}):
        raise ReleaseError(f"credential-like tracked path is not releasable: {name}")
    return "include"


def scan_secret(data: bytes, name: str) -> None:
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            if hashlib.sha256(match.group()).hexdigest() in KNOWN_SYNTHETIC_KEY_HASHES.get(name, set()):
                continue
            raise ReleaseError(f"possible credential material found in tracked file: {name}")


def scan_private_metadata(data: bytes, name: str) -> None:
    """Catch operator paths and addresses in first-party source and docs."""
    if name.startswith("router/"):
        return  # pinned upstream contains its own documented fixture paths
    for match in FIRST_PARTY_HOME.finditer(data):
        segment = (match.group(1) or match.group(2)).decode("ascii").lower()
        if name not in {"server/test_runtime_paths.py", "server/test_jev_server.py"} or segment not in SYNTHETIC_HOME_SEGMENTS:
            raise ReleaseError(f"possible private home path in tracked file: {name}")
    for match in FIRST_PARTY_EMAIL.finditer(data):
        domain = match.group(1).decode("ascii").lower()
        if name != "tests/test_package_release.py" or domain != "example.invalid":
            raise ReleaseError(f"possible private email in tracked file: {name}")


def git_archive(repo: Path, commit: str, target: Path) -> None:
    with target.open("wb") as out:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", commit],
            stdout=out, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode:
        raise ReleaseError(f"git archive failed: {proc.stderr.decode(errors='replace').strip()}")


def write_tar(entries: list[tuple[str, bytes, int]], manifest: bytes, output: Path, epoch: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                dirs: set[str] = set()
                for name, _, _ in entries:
                    parts = PurePosixPath(name).parts[:-1]
                    for i in range(1, len(parts) + 1):
                        dirs.add("/".join(parts[:i]))
                for name in sorted(dirs):
                    info = tarfile.TarInfo(name + "/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = epoch
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info)
                for name, data, mode in entries:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o755 if mode & 0o111 else 0o644
                    info.mtime = epoch
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, _BytesReader(data))
                info = tarfile.TarInfo("RELEASE-MANIFEST.json")
                info.size = len(manifest)
                info.mode = 0o644
                info.mtime = epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, _BytesReader(manifest))


class _BytesReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def build(repo: Path, output: Path, max_bytes: int = DEFAULT_MAX_SOURCE_BYTES) -> dict:
    repo = repo.resolve()
    output = output.expanduser()
    if output.is_symlink():
        raise ReleaseError("refusing to write through an artifact symlink")
    output = output.resolve()
    commit = validate_clean_tree(repo)
    sidecar_sha = output.with_suffix(output.suffix + ".sha256")
    sidecar_prov = output.with_suffix(output.suffix + ".provenance.json")
    if any(path.exists() or path.is_symlink() for path in (output, sidecar_sha, sidecar_prov)):
        raise ReleaseError("refusing to overwrite an existing release artifact or sidecar")
    if output == repo or repo in output.parents:
        for path in (output, sidecar_sha, sidecar_prov):
            proc = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", str(path)], capture_output=True)
            if proc.returncode != 0:
                raise ReleaseError("archive and sidecars inside the repository must all be Git-ignored; prefer an external path")
    epoch_text = run_git(repo, "show", "-s", "--format=%ct", commit)
    epoch = int(epoch_text)
    with tempfile.TemporaryDirectory(prefix="jev-release-") as temp:
        raw_archive = Path(temp) / "source.tar"
        git_archive(repo, commit, raw_archive)
        entries: list[tuple[str, bytes, int]] = []
        skipped: list[str] = []
        total = 0
        with tarfile.open(raw_archive, mode="r:") as source:
            for member in source:
                name = member.name.rstrip("/")
                if member.isdir():
                    continue
                decision = member_policy(name)
                if decision == "omit":
                    skipped.append(name)
                    continue
                if not member.isfile():
                    raise ReleaseError(f"non-regular tracked file is not releasable: {name}")
                total += member.size
                if total > max_bytes:
                    raise ReleaseError(f"source exceeds configured size limit ({max_bytes} bytes)")
                stream = source.extractfile(member)
                if stream is None:
                    raise ReleaseError(f"cannot read Git archive member: {name}")
                data = stream.read()
                if len(data) != member.size:
                    raise ReleaseError(f"truncated Git archive member: {name}")
                scan_secret(data, name)
                scan_private_metadata(data, name)
                entries.append((name, data, member.mode))
        entries.sort(key=lambda item: item[0])
        required = {
            "README.md", "AGENTS.md", "LICENSE", "install.sh", "install.ps1",
            "harness/cli.py", "harness/onboard.py", "server/jev_fetch.mjs",
            "integrations/jev/global/codex_hook.py", "router/LICENSE", "router/src/jev-fallback.mjs",
            "ROUTER_FORK.md", "tests/test_core.py", "docs/INSTALL.md",
            "docs/RELEASING.md",
        }
        names = {name for name, _, _ in entries}
        missing = required - names
        if missing:
            raise ReleaseError("release is missing required files: " + ", ".join(sorted(missing)))
        manifest_obj = {
            "schema": "jev-codex-harness-release/v1",
            "source_commit": commit,
            "source_date_epoch": epoch,
            "files": [
                {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data, _ in entries
            ],
            "omitted_tracked_paths": sorted(skipped),
        }
        manifest = (json.dumps(manifest_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        write_tar(entries, manifest, output, epoch)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    verify_archive(output, manifest_obj, max_bytes)
    sidecar_sha.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    provenance = {
        "schema": "jev-codex-harness-provenance/v1",
        "artifact": output.name,
        "artifact_sha256": digest,
        "artifact_size": output.stat().st_size,
        "source_commit": commit,
        "source_date_epoch": epoch,
        "packager_version": VERSION,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    sidecar_prov.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**provenance, "sha256_file": str(sidecar_sha), "provenance_file": str(sidecar_prov), "files": len(entries)}


def verify_archive(path: Path, manifest_obj: dict, max_bytes: int = DEFAULT_MAX_SOURCE_BYTES) -> None:
    if path.stat().st_size > max_bytes:
        raise ReleaseError("compressed release archive exceeds configured size limit")
    with tarfile.open(path, mode="r:gz") as tar:
        seen: set[str] = set()
        file_names: set[str] = set()
        manifest_data = None
        file_data: dict[str, bytes] = {}
        extracted_size = 0
        for member in tar:
            name = member.name.rstrip("/")
            if name in seen:
                raise ReleaseError(f"duplicate archive member: {name}")
            seen.add(name)
            if name != "RELEASE-MANIFEST.json" and member.isfile():
                if member_policy(name) != "include":
                    raise ReleaseError(f"forbidden path made it into archive: {name}")
                extracted_size += member.size
            if not member.isfile():
                if member.isdir():
                    continue
                raise ReleaseError(f"archive contains non-regular member: {name}")
            file_names.add(name)
            stream = tar.extractfile(member)
            if stream is None:
                raise ReleaseError(f"cannot read archive member: {name}")
            data = stream.read()
            if name == "RELEASE-MANIFEST.json":
                manifest_data = data
            else:
                file_data[name] = data
        if extracted_size > max_bytes:
            raise ReleaseError("expanded release archive exceeds configured size limit")
        if manifest_data is None:
            raise ReleaseError("release manifest is missing")
        if json.loads(manifest_data) != manifest_obj:
            raise ReleaseError("embedded manifest does not match packaged source")
        for item in manifest_obj["files"]:
            data = file_data.get(item["path"])
            if data is None or len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ReleaseError(f"packaged member failed manifest verification: {item['path']}")
        expected = {item["path"] for item in manifest_obj["files"]} | {"RELEASE-MANIFEST.json"}
        if file_names != expected:
            raise ReleaseError("archive member set does not match manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="archive path outside repo (or ignored artifacts directory)")
    parser.add_argument("--max-source-mib", type=int, default=DEFAULT_MAX_SOURCE_BYTES // (1024 * 1024))
    args = parser.parse_args(argv)
    try:
        result = build(args.repo, args.output, args.max_source_mib * 1024 * 1024)
    except (ReleaseError, OSError, tarfile.TarError, ValueError) as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
