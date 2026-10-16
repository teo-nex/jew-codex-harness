"""Deterministic write boundaries shared by portable workers and the Jev hook."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import uuid


def owned_paths(cwd, names):
    cwd = Path(cwd).resolve(strict=True)
    if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("allowed_files must be an array of nonempty paths")
    paths = []
    for name in names:
        path = Path(name)
        target = cwd / path
        if (path.is_absolute() or ".." in path.parts or path == Path(".")
                or not target.resolve().is_relative_to(cwd) or target.is_symlink()):
            raise ValueError("allowed_file escapes cwd or is a symlink")
        if any(p.is_symlink() for p in (target, *target.parents) if p != cwd and p.is_relative_to(cwd)):
            raise ValueError("allowed_file uses a symlink")
        if target.exists() and target.is_file() and target.stat().st_nlink != 1:
            raise ValueError("allowed_file is hard-linked")
        if any(target == p or target.is_relative_to(p) or p.is_relative_to(target) for p in paths):
            raise ValueError("Duplicate or overlapping allowed_files")
        paths.append(target)
    return paths


def patch_allowed(task, patch):
    if task.get("read_only") or not isinstance(patch, str):
        return False
    cwd = Path(task["cwd"]).resolve(strict=True)
    try:
        allowed = owned_paths(cwd, task["allowed_files"])
        targets = re.findall(r"(?m)^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", patch)
        if not targets or not patch.startswith("*** Begin Patch\n") or not patch.rstrip().endswith("*** End Patch"):
            return False
        for _, name in targets:
            path = Path(name)
            target = path if path.is_absolute() else cwd / path
            if not target.resolve().is_relative_to(cwd):
                return False
            owned_paths(cwd, [str(target.relative_to(cwd))])
            if not any(target == p or (p.is_dir() and target.is_relative_to(p)) for p in allowed):
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def readonly_command(command):
    """Allow a small shell read surface; interpreters and output options fail closed."""
    if not isinstance(command, str) or re.search(r"[\r\n;&|<>`$(){}]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    options = {
        "pwd": set(), "ls": {"-a", "-l", "-la", "-al", "-1", "--"},
        "cat": {"--"}, "head": {"-n", "-c", "--"}, "tail": {"-n", "-c", "--"},
        "rg": {"-n", "-i", "-l", "-F", "--files", "--hidden", "--", "-g", "--glob", "-e", "-m", "--max-count"},
        "get-content": {"-literalpath", "-path", "-raw", "-totalcount", "-tail"},
        "get-childitem": {"-literalpath", "-path", "-name", "-force"},
        "get-location": set(), "select-string": {"-literalpath", "-path", "-pattern", "-simplematch"},
    }
    name = words[0].lower()
    if name not in options:
        return False
    return all(not word.startswith("-") or word.lower() in options[name] for word in words[1:])


def bound_task():
    """The runner pins a private manifest digest in the hook process environment."""
    name = os.environ.get("JEV_WORKER_MANIFEST")
    if not name:
        return None
    path = Path(name)
    root = Path(os.environ["JEV_WORKER_STATE_DIR"]).resolve(strict=True)
    if (path.is_symlink() or path.parent.is_symlink() or path.name != "task.json"
            or path.resolve().parent.parent != root):
        raise ValueError("Worker manifest outside bound state directory")
    uuid.UUID(path.parent.name)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != os.environ.get("JEV_WORKER_MANIFEST_SHA256"):
        raise ValueError("Worker manifest changed")
    task = json.loads(raw)
    if task.get("task_id") != path.parent.name:
        raise ValueError("Worker identity mismatch")
    owned_paths(task["cwd"], task["allowed_files"])
    return task
