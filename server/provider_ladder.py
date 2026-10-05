"""Persistent provider order, sticky accounts and bounded primary recovery."""

import json
import hashlib
import math
import os
from pathlib import Path
import re
import time
try:
    from .portable_lock import locked_file
    from .reasoning_effort import validate_reasoning_profiles
    from .project_policy import validate_project_fields
except ImportError:  # launched as a script from server/
    from portable_lock import locked_file
    from reasoning_effort import validate_reasoning_profiles
    from project_policy import validate_project_fields

STAGES = ("plus", "gemini", "opus", "glm", "deepseek", "wally", "main", "exhausted")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NATIVE_MODELS = ("gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-astra")


def validate_ordered_config(value):
    if set(value) - {"version", "providers", "reasoning_profiles", "project_policies", "project_scopes"}:
        raise ValueError("Ordered providers cannot be mixed with legacy ladder fields")
    if value.get("version", 2) != 2 or isinstance(value.get("version"), bool):
        raise ValueError("Ordered provider config version must be 2")
    providers = value.get("providers")
    if not isinstance(providers, list) or not 1 <= len(providers) <= 32:
        raise ValueError("providers must contain between 1 and 32 routes")
    result, ids = [], set()
    for provider in providers:
        if not isinstance(provider, dict) or set(provider) - {"id", "transport", "model", "models", "connection_ids", "billing"}:
            raise ValueError("Invalid provider fields")
        identity = provider.get("id")
        if (not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity)
                or identity == "exhausted" or identity in ids):
            raise ValueError("Provider IDs must be unique lowercase identifiers")
        ids.add(identity)
        transport = provider.get("transport", "omniroute")
        if "billing" in provider and provider["billing"] not in ("free", "subscription", "paid"):
            raise ValueError("billing must be free, subscription or paid")
        if transport not in ("omniroute", "native"):
            raise ValueError("Provider transport must be omniroute or native")
        model, models = provider.get("model"), provider.get("models")
        if "model" in provider and "models" in provider:
            raise ValueError("Specify model or models, not both")
        if "model" in provider and (not isinstance(model, str) or not model.strip() or model != model.strip()):
            raise ValueError("Provider model must be an exact non-empty model ID")
        if "models" in provider:
            if (not isinstance(models, dict) or set(models) != set(NATIVE_MODELS)
                    or any(not isinstance(m, str) or not m.strip() or m != m.strip() for m in models.values())):
                raise ValueError("Provider models must map every Jev native model to an exact model ID")
        if model is None and models is None and transport != "native":
            raise ValueError("OmniRoute provider needs model or models")
        accounts = provider.get("connection_ids", [])
        if (not isinstance(accounts, list) or len(accounts) > 32
                or any(not isinstance(a, str) or not a.strip() or a != a.strip() for a in accounts)
                or len(set(accounts)) != len(accounts)
                or ("connection_ids" in provider and not accounts)
                or (transport == "native" and accounts)):
            raise ValueError("connection_ids must be unique non-empty IDs for an OmniRoute provider")
        entry = {**provider, "transport": transport}
        if models is not None:
            entry["models"] = dict(models)
        if "connection_ids" in provider:
            entry["connection_ids"] = list(accounts)
        result.append(entry)
    profiles = validate_reasoning_profiles(value.get("reasoning_profiles", {}))
    project_fields = validate_project_fields(value)
    return {"version": 2, "providers": result, "reasoning_profiles": profiles,
            **(project_fields if "project_policies" in value or "project_scopes" in value else {})}


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("provider ladder config must be an object")
    if "providers" in value:
        return validate_ordered_config(value)
    value = dict(value)
    plus = value.get("plus_connection_id")
    gemini = value.get("gemini_connection_ids")
    if not isinstance(plus, str) or not plus:
        raise ValueError("Plus connection missing")
    if not isinstance(gemini, list) or not gemini or any(
        not isinstance(item, str) or not item for item in gemini
    ) or len(set(gemini)) != len(gemini):
        raise ValueError("Gemini accounts missing or duplicated")
    for name in ("gemini_model", "glm_model", "deepseek_model", "main_model"):
        if not isinstance(value.get(name), str) or not value[name]:
            raise ValueError(name + " missing")
    wally_model = value.get("wally_model")
    if wally_model is not None and (not isinstance(wally_model, str)
                                    or not wally_model.startswith("wally/")):
        raise ValueError("wally_model invalid")
    opus_model = value.get("opus_model")
    opus_accounts = value.get("opus_connection_ids")
    if opus_model is not None or opus_accounts is not None:
        if not isinstance(opus_model, str) or not opus_model.startswith("antigravity/claude-opus-"):
            raise ValueError("opus_model invalid")
        if (not isinstance(opus_accounts, list) or not opus_accounts
                or any(not isinstance(item, str) or item not in gemini for item in opus_accounts)
                or len(set(opus_accounts)) != len(opus_accounts)):
            raise ValueError("opus_connection_ids invalid")
    if "reasoning_profiles" in value:
        reasoning_profiles = value["reasoning_profiles"]
        if reasoning_profiles is None or not isinstance(reasoning_profiles, dict):
            raise ValueError("reasoning_profiles must be an object")
        value["reasoning_profiles"] = validate_reasoning_profiles(reasoning_profiles)
    return value


def plus_model(tier, effort):
    if tier not in NATIVE_MODELS or effort not in EFFORTS:
        raise ValueError("Invalid Plus model or reasoning effort")
    if tier == "gpt-6-astra":
        return tier
    family = "luna" if "luna" in tier else "terra" if "terra" in tier else "sol"
    return f"codex/gpt-5.6-{family}-{effort}"


class Ladder:
    def __init__(self, config, state_path):
        self.config = validate_config(config)
        self.providers = self.config.get("providers")
        self.fingerprint = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = self.path.with_suffix(".lock")

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("provider ladder state is not an object")
        return value

    def _save(self, value):
        tmp = self.path.with_name(self.path.name + ".tmp-" + str(os.getpid()))
        with tmp.open("x") as output:
            os.chmod(tmp, 0o600)
            json.dump(value, output, separators=(",", ":"))
        os.replace(tmp, self.path)

    def _with_state(self, operation):
        with locked_file(self.lock):
            value = self._load()
            result, dirty = operation(value)
            if dirty:
                self._save(value)
            return result

    def route(self, scope, tier, effort, *, recover_primary=True):
        if not isinstance(scope, str) or not scope:
            raise ValueError("stable thread scope required")
        if self.providers is not None:
            return self._ordered_route(scope, tier, effort, recover_primary)
        if tier == "gpt-6-astra":
            return self.native_route(tier, effort)
        def operation(state):
            thread = state.setdefault(scope, {"stage": "plus", "gemini_index": 0})
            now = time.time()
            thread["seen_at"] = int(now)
            stage = thread.get("stage")
            if stage not in STAGES:
                stage = thread["stage"] = "plus"
            if stage == "opus" and not self.config.get("opus_connection_ids"):
                stage = thread["stage"] = "glm"
            if stage == "wally" and not self.config.get("wally_model"):
                stage = thread["stage"] = "main"
            circuit = state.get("_plus_circuit")
            circuit_until = circuit.get("until") if isinstance(circuit, dict) else None
            if not isinstance(circuit_until, (int, float)) or circuit_until <= now:
                circuit_until = None
            retry_at = thread.get("plus_retry_at")
            if recover_primary and stage != "plus" and isinstance(retry_at, (int, float)) and now >= retry_at:
                if circuit_until:
                    thread["plus_retry_at"] = circuit_until
                else:
                    # A recovered Plus window takes priority again. Preserve
                    # the previous provider/account if this probe fails.
                    thread["resume_stage"] = stage
                    stage = thread["stage"] = "plus"
                    thread.pop("plus_retry_at", None)
            if stage == "plus" and circuit_until:
                stage = thread["stage"] = thread.pop("resume_stage", None) or "gemini"
                thread["plus_retry_at"] = circuit_until
            if stage == "main" and self.config.get("wally_model") and not thread.get("wally_seen"):
                # Threads already on Luna when this provider was installed get
                # one chance at the new stage without losing their history.
                stage = thread["stage"] = "wally"
                thread["wally_seen"] = True
            index = max(0, int(thread.get("gemini_index") or 0))
            opus_index = max(0, int(thread.get("opus_index") or 0))
            if stage == "plus":
                model = plus_model(tier, effort)
                account = self.config["plus_connection_id"]
            elif stage == "gemini" and index < len(self.config["gemini_connection_ids"]):
                model = self.config["gemini_model"]
                account = self.config["gemini_connection_ids"][index]
            elif stage == "opus" and opus_index < len(self.config.get("opus_connection_ids") or []):
                model = self.config["opus_model"]
                account = self.config["opus_connection_ids"][opus_index]
            elif stage in ("glm", "deepseek", "wally", "main"):
                model = self.config[stage + "_model"]
                account = None
            else:
                stage = "exhausted"
                model = account = None
            return {"stage": stage, "model": model, "account": account,
                    "gemini_index": index, "opus_index": opus_index, "effort": effort}, True
        return self._with_state(operation)

    def advance(self, scope, route, reason, reset_at=None, cooldown_seconds=900):
        """CAS: only the failed route may advance its own thread."""
        if reason not in ("quota", "unavailable", "invalid_response"):
            raise ValueError("unsupported fallback reason")
        if self.providers is not None:
            return self._ordered_advance(scope, route, reason, reset_at, cooldown_seconds)
        def operation(state):
            thread = state.get(scope)
            if not isinstance(thread, dict) or thread.get("stage") != route.get("stage"):
                return False, False
            if route.get("stage") in ("gemini", "opus"):
                stage = route["stage"]
                ids = self.config["gemini_connection_ids"] if stage == "gemini" else self.config.get("opus_connection_ids") or []
                key = "gemini_index" if stage == "gemini" else "opus_index"
                index = int(thread.get(key) or 0)
                if index != route.get(key) or index >= len(ids) or route.get("account") != ids[index]:
                    return False, False
                index += 1
                thread[key] = index
                if index < len(ids):
                    thread["stage"] = stage
                elif stage == "gemini":
                    thread["stage"] = "opus" if self.config.get("opus_connection_ids") else "glm"
                    thread["opus_index"] = 0
                else:
                    thread["stage"] = "glm"
            else:
                next_stage = {"plus": "gemini", "glm": "deepseek",
                              "deepseek": "wally" if self.config.get("wally_model") else "main",
                              "wally": "main", "main": "exhausted"}.get(route.get("stage"))
                if not next_stage or (route.get("stage") == "plus" and route.get("account") != self.config["plus_connection_id"]):
                    return False, False
                if route.get("stage") == "plus":
                    next_stage = thread.pop("resume_stage", None) or "gemini"
                    now = time.time()
                    # Missing reset headers are common on compatible gateways.
                    # A bounded periodic probe allows Plus to recover without
                    # pinning the thread to a fallback for its whole lifetime.
                    until = (
                        reset_at if isinstance(reset_at, (int, float)) and now + 30 <= reset_at <= now + 7 * 86400
                        else now + max(30, min(int(cooldown_seconds), 3600))
                    )
                    thread["plus_retry_at"] = until
                    state["_plus_circuit"] = {"until": until, "reason": reason}
                if route.get("stage") == "wally":
                    thread["wally_seen"] = True
                thread["stage"] = next_stage
            thread["last_failure"] = reason
            thread["seen_at"] = int(time.time())
            return True, True
        return self._with_state(operation)

    def note_success(self, scope, route):
        """A confirmed primary response reopens it for new and parked threads."""
        if self.providers is not None:
            return self._ordered_success(scope, route)
        if route.get("stage") != "plus":
            return
        def operation(state):
            if isinstance(state.get(scope), dict) and state[scope].get("stage") == "plus":
                removed = state.pop("_plus_circuit", None) is not None
                return removed, removed
            return False, False
        return self._with_state(operation)

    @property
    def attempt_limit(self):
        if self.providers is not None:
            return sum(max(1, len(p.get("connection_ids", []))) for p in self.providers)
        return len(self.config["gemini_connection_ids"]) + len(self.config.get("opus_connection_ids") or []) + 5

    def native_route(self, tier, effort):
        if self.providers is None:
            return {"stage": "main", "model": tier if tier == "gpt-6-astra" else self.config["main_model"], "account": None,
                    "effort": effort, "transport": "native"}
        for index, provider in enumerate(self.providers):
            if provider["transport"] == "native":
                return self._ordered_choice(index, 0, tier, effort)
        return None

    def _ordered_choice(self, index, account_index, tier, effort):
        if index >= len(self.providers):
            return {"stage": "exhausted", "model": None, "account": None}
        provider = self.providers[index]
        model = provider.get("model") or provider.get("models", {}).get(tier) or tier
        accounts = provider.get("connection_ids", [])
        return {"stage": provider["id"], "model": model,
                "account": accounts[account_index] if accounts else None,
                "effort": effort, "transport": provider["transport"],
                "provider_index": index, "account_index": account_index,
                "config_hash": self.fingerprint}

    def _ordered_position_valid(self, index, account):
        if (type(index) is not int or not 0 <= index <= len(self.providers)
                or type(account) is not int or account < 0):
            return False
        count = len(self.providers[index].get("connection_ids", [])) if index < len(self.providers) else 0
        return account < max(1, count)

    def _ordered_route(self, scope, tier, effort, recover_primary):
        if tier not in NATIVE_MODELS or effort not in EFFORTS:
            raise ValueError("Invalid Jev model or reasoning effort")
        def operation(state):
            thread = state.get(scope)
            if not isinstance(thread, dict) or thread.get("config_hash") != self.fingerprint:
                thread = state[scope] = {"config_hash": self.fingerprint, "provider_index": 0, "account_index": 0}
            index, account = thread.get("provider_index"), thread.get("account_index")
            if not self._ordered_position_valid(index, account):
                raise ValueError("Invalid ordered provider state")
            generation = thread.get("generation", 0)
            retry_at = thread.get("primary_retry_at")
            resume = thread.get("resume_route")
            if (type(generation) is not int or generation < 0
                    or (retry_at is not None and (type(retry_at) not in (int, float) or not math.isfinite(retry_at)))
                    or (resume is not None and (not isinstance(resume, (list, tuple)) or len(resume) != 2
                        or not self._ordered_position_valid(*resume) or resume[0] == 0))):
                raise ValueError("Invalid ordered provider recovery state")
            now = time.time()
            circuit = state.get("_ordered_primary_circuit", {})
            if not isinstance(circuit, dict):
                raise ValueError("Invalid ordered provider circuit")
            until = circuit.get("until", 0) if circuit.get("config_hash") == self.fingerprint else 0
            if type(until) not in (int, float) or not math.isfinite(until):
                raise ValueError("Invalid ordered provider cooldown")
            if recover_primary and index > 0 and retry_at is not None and now >= retry_at and now >= until:
                thread["resume_route"] = (index, account)
                index = account = 0
                thread.pop("primary_retry_at", None)
            elif index == 0 and now < until:
                index, account = thread.pop("resume_route", (1, 0))
                thread["primary_retry_at"] = until
            if (index, account) != (thread["provider_index"], thread["account_index"]):
                thread["generation"] = thread.get("generation", 0) + 1
            thread.update(provider_index=index, account_index=account, seen_at=int(now))
            chosen = self._ordered_choice(index, account, tier, effort)
            chosen["generation"] = thread.get("generation", 0)
            return chosen, True
        return self._with_state(operation)

    def _ordered_advance(self, scope, route, reason, reset_at, cooldown_seconds):
        def operation(state):
            thread = state.get(scope)
            if (not isinstance(thread, dict) or route.get("config_hash") != self.fingerprint
                    or thread.get("config_hash") != self.fingerprint
                    or route.get("generation") != thread.get("generation", 0)
                    or (thread.get("provider_index"), thread.get("account_index")) !=
                       (route.get("provider_index"), route.get("account_index"))):
                return False, False
            index, account = thread["provider_index"], thread["account_index"]
            if index >= len(self.providers):
                return False, False
            accounts = self.providers[index].get("connection_ids", [])
            if route.get("account") != (accounts[account] if accounts else None):
                return False, False
            account += 1
            if account >= max(1, len(accounts)):
                if index == 0:
                    now = time.time()
                    until = (reset_at if type(reset_at) in (int, float) and now + 30 <= reset_at <= now + 7 * 86400
                             else now + max(30, min(int(cooldown_seconds), 3600)))
                    state["_ordered_primary_circuit"] = {"config_hash": self.fingerprint, "until": until}
                    thread["primary_retry_at"] = until
                    index, account = thread.pop("resume_route", (1, 0))
                    if index >= len(self.providers):
                        index, account = 1, 0
                else:
                    index, account = index + 1, 0
            thread.update(provider_index=index, account_index=account,
                          generation=thread.get("generation", 0) + 1,
                          last_failure=reason, seen_at=int(time.time()))
            return True, True
        return self._with_state(operation)

    def _ordered_success(self, scope, route):
        def operation(state):
            thread = state.get(scope)
            if (not isinstance(thread, dict) or route.get("config_hash") != self.fingerprint
                    or thread.get("config_hash") != self.fingerprint
                    or route.get("generation") != thread.get("generation", 0)
                    or (thread.get("provider_index"), thread.get("account_index")) !=
                       (route.get("provider_index"), route.get("account_index"))):
                return False, False
            if route["provider_index"] == 0:
                thread.pop("resume_route", None)
                state.pop("_ordered_primary_circuit", None)
            return True, True
        return self._with_state(operation)
