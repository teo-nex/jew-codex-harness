"""Read-only, bounded catalog lookup on the existing loopback gateway."""

import http.client
import json
import os
import re

from . import core


def fetch(auth_file):
    if auth_file is None:
        raise core.InstallError("Set protected OmniRoute auth path for catalog lookup")
    path = core.protected_file(auth_file, "OmniRoute auth")
    auth = json.loads(path.read_text())
    gateway = auth.get("omniroute") if isinstance(auth, dict) else None
    key = gateway.get("key") if isinstance(gateway, dict) else None
    if not isinstance(key, str) or not key:
        raise core.InstallError("OmniRoute credential unavailable")
    host = os.environ.get("JEV_OMNIROUTE_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise core.InstallError("Catalog lookup requires a loopback gateway")
    conn = http.client.HTTPConnection(host, int(os.environ.get("JEV_OMNIROUTE_PORT", "20128")), timeout=10)
    try:
        conn.request("GET", "/v1/models", headers={"Authorization": "Bearer " + key})
        response = conn.getresponse()
        if response.status != 200:
            raise core.InstallError(f"OmniRoute catalog returned HTTP {response.status}")
        data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise core.InstallError("OmniRoute catalog too large")
        return normalize(json.loads(data))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise core.InstallError("OmniRoute catalog unavailable or malformed") from exc
    finally:
        conn.close()


def normalize(value):
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Catalog must contain a data array")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("Invalid catalog model")
        if row["id"] in result:
            raise ValueError("Duplicate catalog model")
        # Never expose arbitrary gateway fields or reflected credential data.
        result[row["id"]] = {"id": row["id"]}
        target = row.get("target_model")
        if isinstance(target, str) and target:
            result[row["id"]]["target_model"] = target
    return [result[key] for key in sorted(result)]


def _family(model):
    match = re.search(r"gpt-(\d+(?:\.\d+)?)-(luna|terra|sol|astra)", model)
    return match.group(0) if match else None


def warnings(config, models):
    available = {row["id"]: row for row in models}
    result = []
    for provider in config.get("providers", []):
        if provider["transport"] != "omniroute":
            continue
        mapping = provider.get("models") or {None: provider.get("model")}
        for selected, destination in mapping.items():
            if destination not in available:
                result.append({"provider": provider["id"], "model": destination, "warning": "not_advertised"})
            target = available.get(destination, {}).get("target_model") or destination
            source_family = _family(selected) if selected else _family(destination)
            target_family = _family(target)
            if source_family and target_family and source_family != target_family:
                result.append({"provider": provider["id"], "model": destination,
                               "target_model": target, "warning": "family_substitution"})
    return result


def choose(read, models, question):
    if not models:
        raise core.InstallError("Gateway catalog is empty")
    options = "\n".join(f"{index}: {row['id']}" for index, row in enumerate(models, 1))
    answer = read(options + "\n" + question + " (model number): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(models):
        raise core.InstallError("Choose a model number from the catalog")
    return models[int(answer) - 1]["id"]
