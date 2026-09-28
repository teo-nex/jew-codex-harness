"""One-thread provider priority and sticky Gemini account state."""

import json
import os
from pathlib import Path
import time
try:
    from .portable_lock import locked_file
    from .reasoning_effort import validate_reasoning_profiles
except ImportError:  # launched as a script from server/
    from portable_lock import locked_file
    from reasoning_effort import validate_reasoning_profiles

STAGES = ("plus", "gemini", "opus", "glm", "deepseek", "wally", "main", "exhausted")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("provider ladder config must be an object")
    plus = value.get("plus_connection_id")
    gemini = value.get("gemini_connection_ids")
    if not isinstance(plus, str) or not plus:
        raise ValueError("Plus connection missing")
    if not isinstance(gemini, list) or not gemini or len(set(gemini)) != len(gemini) or any(
        not isinstance(item, str) or not item for item in gemini
    ):
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
                or len(set(opus_accounts)) != len(opus_accounts)
                or any(not isinstance(item, str) or item not in gemini for item in opus_accounts)):
            raise ValueError("opus_connection_ids invalid")
    if "reasoning_profiles" in value:
        reasoning_profiles = value["reasoning_profiles"]
        if reasoning_profiles is None or not isinstance(reasoning_profiles, dict):
            raise ValueError("reasoning_profiles must be an object")
        value["reasoning_profiles"] = validate_reasoning_profiles(reasoning_profiles)
    return value


def plus_model(tier, effort):
    family = ("luna" if "luna" in tier else "terra" if "terra" in tier else "sol")
    depth = effort if effort in EFFORTS else "high"
    return f"codex/gpt-5.6-{family}-{depth}"


class Ladder:
    def __init__(self, config, state_path):
        self.config = validate_config(config)
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

    def route(self, scope, tier, effort):
        if not isinstance(scope, str) or not scope:
            raise ValueError("stable thread scope required")
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
            if stage != "plus" and isinstance(retry_at, (int, float)) and now >= retry_at:
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
        """A confirmed Plus response reopens Plus for new and parked threads."""
        if route.get("stage") != "plus":
            return
        def operation(state):
            if isinstance(state.get(scope), dict) and state[scope].get("stage") == "plus":
                removed = state.pop("_plus_circuit", None) is not None
                return removed, removed
            return False, False
        return self._with_state(operation)
