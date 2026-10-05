"""Operator-owned project bindings, not instructions from request text."""

from pathlib import PurePosixPath, PureWindowsPath
import re


def validate_project_fields(config):
    policies, scopes = config.get("project_policies", {}), config.get("project_scopes", {})
    if not isinstance(policies, dict) or not isinstance(scopes, dict) or len(policies) > 256 or len(scopes) > 4096:
        raise ValueError("Invalid project policy inventory")
    result = {}
    for root, policy in policies.items():
        if (not isinstance(root, str) or not (PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute())
                or not isinstance(policy, dict) or set(policy) -
                {"native_only", "allowed_providers", "no_paid_fallback", "require_astra_final"}):
            raise ValueError("Invalid project policy")
        for key in ("native_only", "no_paid_fallback", "require_astra_final"):
            if key in policy and type(policy[key]) is not bool:
                raise ValueError("Project policy switches must be boolean")
        allowed = policy.get("allowed_providers")
        if allowed is not None and (not isinstance(allowed, list)
                or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item) for item in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValueError("allowed_providers must be distinct provider IDs")
        result[root] = {**policy, **({"allowed_providers": list(allowed)} if allowed is not None else {})}
    for scope, root in scopes.items():
        if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{16}", scope) or not isinstance(root, str) or root not in result:
            raise ValueError("Project scopes must bind logged scope hashes to configured roots")
    return {"project_policies": result, "project_scopes": dict(scopes)}


def resolve(config, scope):
    if not config.get("project_policies"):
        return None
    root = config.get("project_scopes", {}).get(scope)
    if root is None:
        # An unbound session cannot escape a project's restrictive policy.
        return {"native_only": True, "binding": "unbound_native_only"}
    return {**config["project_policies"][root], "binding": "operator_bound"}


def final_required(policy, payload, decision):
    metadata = payload.get("metadata")
    return bool(policy and policy.get("require_astra_final") and
                ((isinstance(metadata, dict) and metadata.get("jev_phase") == "final_review")
                 or (decision and decision.get("astra_policy") == "astra")))


def restrict(config, policy, final=False):
    if not policy:
        return config
    if "providers" not in config:
        raise ValueError("Project policies require ordered providers")
    allowed = policy.get("allowed_providers")
    providers = []
    for entry in config["providers"]:
        native = entry["transport"] == "native"
        if not native and (policy.get("native_only") or (allowed is not None and entry["id"] not in allowed)
                           or (policy.get("no_paid_fallback") and entry.get("billing") not in ("free", "subscription"))):
            continue
        destination = entry.get("model") or entry.get("models", {}).get("gpt-6-astra") or "gpt-6-astra"
        if final and (not native or destination != "gpt-6-astra"):
            continue
        providers.append(entry)
    if not providers:
        raise ValueError("No configured route satisfies project policy")
    return {**config, "providers": providers}
