"""Local service access controls and bounded, owner-only diagnostic files."""
import hmac
import os
import stat
import subprocess
import threading
from http.server import ThreadingHTTPServer


def resolve_runtime_paths(environ=None, home=None):
    """Resolve the same configurable state hierarchy as the embedded router."""
    env = os.environ if environ is None else environ
    owner_home = os.path.expanduser("~") if home is None else str(home)
    codex_home = os.path.expanduser(
        env.get("CODEX_HOME") or os.path.join(owner_home, ".codex")
    )
    state = os.path.expanduser(
        env.get("MODEL_ROUTER_STATE_DIR")
        or env.get("CODEX_ROUTER_STATE_DIR")
        or env.get("KIMI_CODEX_STATE_DIR")
        or os.path.join(codex_home, "codex-router")
    )
    return owner_home, codex_home, state


HOME, CODEX_HOME, STATE = resolve_runtime_paths()
AUTH_PATH = os.path.join(STATE, "generic-provider-credentials", "jev.key")
MAX_LOG_BYTES = 8 * 1024 * 1024
_file_lock = threading.Lock()
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def windows_acl_private(path):
    """Check a credential's DACL; Windows mode bits do not describe sharing."""
    if os.path.islink(path):
        return False
    script = (
        "$ErrorActionPreference='Stop'; "
        "try { $acl=[System.IO.File]::GetAccessControl($env:JEV_ACL_CHECK_PATH); "
        "$self=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$allowed=@($self,'S-1-5-18','S-1-5-32-544'); "
        "$rules=$acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]); "
        "foreach ($ace in $rules) { "
        "if ($ace.AccessControlType -eq 'Allow' -and "
        "$ace.IdentityReference.Value -notin $allowed) { exit 3 } }; exit 0 } "
        "catch { exit 2 }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "JEV_ACL_CHECK_PATH": os.fspath(path)},
            capture_output=True, check=False, timeout=45,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def private_regular(info, path=None):
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name == "nt":
        return path is not None and windows_acl_private(path)
    return not (info.st_mode & 0o077) and info.st_uid == os.getuid()


def restrict_file(fd):
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def local_secret():
    """Use the parent's protected generic-provider credential; fail closed."""
    try:
        if os.path.islink(AUTH_PATH):
            return ""
        fd = os.open(AUTH_PATH, os.O_RDONLY | NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not private_regular(info, AUTH_PATH):
                return ""
            return handle.read(4096).strip()
    except (OSError, UnicodeError):
        return ""


def authorized(header, secret):
    return bool(secret) and hmac.compare_digest(
        str(header or "").encode(), ("Bearer " + secret).encode()
    )


def append_private(path, text, max_bytes=MAX_LOG_BYTES):
    """Bound retention to current + one backup. Never follow file symlinks."""
    data = text.encode("utf-8")
    if len(data) > max_bytes:
        data = data[:max_bytes].decode("utf-8", "ignore").encode()
    with _file_lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600)
        try:
            restrict_file(fd)
            if os.fstat(fd).st_size + len(data) > max_bytes:
                backup = path + ".1"
                os.close(fd)
                fd = None
                os.replace(path, backup)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as handle:
                fd = None
                handle.write(data)
        finally:
            if fd is not None:
                os.close(fd)


def protect_logs(paths):
    """Secure existing captures without reading or exposing their contents."""
    for path in paths:
        for candidate in (path, path + ".1"):
            try:
                fd = os.open(candidate, os.O_RDONLY | NOFOLLOW)
                try:
                    restrict_file(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass


class LocalServer(ThreadingHTTPServer):
    """Bound concurrent connections, including clients waiting to send a body."""
    daemon_threads = True

    def __init__(self, *args, max_connections=32, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
