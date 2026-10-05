"""Journal-aware route edits; no credentials or service lifecycle changes."""

import copy
import hashlib
import json
import re
from pathlib import Path
import uuid

from server.portable_lock import locked_file
from server.provider_ladder import validate_config
from . import core


RELATIVE = "jev-harness/ladder-config.json"


def _bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _recover(home):
    pending = home / "jev-harness/routes-pending.json"
    if not pending.exists():
        return
    core.protected_file(pending, "route transaction")
    txn = json.loads(pending.read_text())
    if not isinstance(txn, dict):
        raise core.InstallError("Invalid route transaction receipt")
    for prefix in ("before", "after"):
        saved = txn.get(prefix + "_journal")
        content = txn.get(prefix + "_text")
        digest = txn.get(prefix + "_hash")
        if (not isinstance(content, str) or digest != _hash(content.encode())
                or not isinstance(saved, dict) or saved.get("codex_home") != str(home)
                or saved.get("phase") != "complete" or not isinstance(saved.get("owned"), dict)
                or saved["owned"].get(RELATIVE) != digest):
            raise core.InstallError("Invalid route transaction receipt")
        validate_config(json.loads(content))
    journal = core._read_journal(home)
    current = core._digest(home / RELATIVE)
    if journal not in (txn["before_journal"], txn["after_journal"]):
        raise core.InstallError("Route transaction journal changed; exact review required")
    if current not in (txn["before_hash"], txn["after_hash"]):
        raise core.InstallError("Route transaction config changed; exact review required")
    # An interrupted edit is rolled forward from its protected durable receipt.
    if current != txn["after_hash"]:
        core._atomic(home / RELATIVE, txn["after_text"].encode())
    if journal != txn["after_journal"]:
        core._journal(home / "jev-harness", txn["after_journal"], journal)
    pending.unlink()


def _owned(home):
    journal = core._read_journal(home)
    if journal["phase"] != "complete":
        raise core.InstallError("Route edits require a complete owned installation")
    manifest = home / "jev-harness/manifest.json"
    if core._digest(manifest) != journal.get("manifest_sha256"):
        raise core.InstallError("Installation manifest changed")
    if RELATIVE not in journal.get("owned", {}):
        raise core.InstallError("Native-only installation has no hot-editable ladder; migration required")
    if core._managed_inventory(home, Path(journal["definition_path"])) != core._expected_managed(journal, home):
        raise core.InstallError("Owned installation changed; refusing route edit")
    return journal


def manage(home, action, config=None, order=None, scope=None, project=None):
    home = Path(home).expanduser().resolve()
    state = home / "jev-harness"
    if not state.is_dir() or state.is_symlink():
        raise core.InstallError("No owned installation")
    lock = state / "routes.lock"
    if lock.is_symlink():
        raise core.InstallError("Refusing symlink at route lock")
    with locked_file(lock):
        _recover(home)
        journal = _owned(home)
        original = (home / RELATIVE).read_bytes()
        current = validate_config(json.loads(original))
        if action == "show":
            return {"config": current, "sha256": _hash(original),
                    "revision": journal.get("route_revision")}
        if action == "check":
            candidate = validate_config(json.loads(core.ladder_file(config).read_text())) if config else current
            return {"valid": True, "config": candidate}
        if action == "bind":
            root = str(Path(project).expanduser().resolve())
            if root not in current.get("project_policies", {}):
                raise core.InstallError("Configure this project policy before binding a session")
            current.setdefault("project_scopes", {})[scope] = root
            data = _bytes(validate_config(current))
        elif action == "rollback":
            revision = journal.get("route_revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{32}", revision):
                raise core.InstallError("No previous route revision")
            receipt = state / "routes-history" / (revision + ".json")
            core.protected_file(receipt, "route backup")
            previous = json.loads(receipt.read_text())
            if (not isinstance(previous, dict) or previous.get("after_hash") != _hash(original)
                    or not isinstance(previous.get("before_text"), str)
                    or previous.get("before_hash") != _hash(previous["before_text"].encode())):
                raise core.InstallError("Route backup does not match current config")
            data = previous["before_text"].encode()
            validate_config(json.loads(data))
        elif action == "reorder":
            providers = current.get("providers")
            if providers is None or len(order) != len(providers) or set(order) != {p["id"] for p in providers}:
                raise core.InstallError("Order must name every configured provider exactly once")
            by_id = {p["id"]: p for p in providers}
            current["providers"] = [by_id[item] for item in order]
            data = _bytes(current)
        elif action == "apply":
            data = _bytes(validate_config(json.loads(core.ladder_file(config).read_text())))
        else:
            raise ValueError("Unknown route action")
        if data == original:
            return {"changed": False, "sha256": _hash(data)}
        revision = uuid.uuid4().hex
        after = copy.deepcopy(journal)
        after["owned"][RELATIVE] = _hash(data)
        after["route_revision"] = revision
        txn = {"before_text": original.decode(), "after_text": data.decode(),
               "before_hash": _hash(original), "after_hash": _hash(data),
               "before_journal": journal, "after_journal": after}
        backup = state / "routes-history" / (revision + ".json")
        core._atomic(backup, _bytes(txn))
        core._atomic(state / "routes-pending.json", _bytes(txn))
        _recover(home)
        return {"changed": True, "sha256": _hash(data), "revision": revision,
                "backup": str(backup), "restart_required": False}
