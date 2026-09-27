"""Direct, typed System One API client. Failure never changes execution policy."""
import hashlib
import json
import os
from pathlib import Path
import stat
import re
import urllib.error
import urllib.request

MODEL = "jev-1.13.0"
URL = "https://api.typesafe.ai/v1/systemone"
def local_url():
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        port = json.loads((home / "jev-harness/manifest.json").read_text()).get("port")
        if isinstance(port, int) and 1024 <= port <= 65535:
            return f"http://127.0.0.1:{port}/ask"
    except (OSError, ValueError):
        pass
    return "http://127.0.0.1:4319/ask"


LOCAL_URL = local_url()
RUBRIC_VERSION = "worker-v2"
TASK_QUESTION = {"type": "choice", "instructions": "Classify this request by the strongest capability required. A short prompt can still need research or repo work; use simple only when one brief answer needs no tools or current facts.", "criteria": {
    "mechanical": "Exact, deterministic file or command operation with no semantic judgment",
    "simple": "One self-contained question answered briefly without repository work, tools, or current external facts",
    "routine": "Localized coding or diagnosis with clear acceptance criteria",
    "complex": "Cross-file design, uncertain behavior, or difficult debugging",
    "unclear": "Goal, ownership, or acceptance is too ambiguous to execute safely",
}}
RELEVANCE_QUESTION = {"type": "choice", "instructions": "Is this old context excerpt necessary to complete the current goal? Keep if it includes instructions, an unresolved error, a current diff, or uncertain evidence.", "criteria": {
    "keep": "Necessary or uncertain; preserve the excerpt",
    "prune": "Clearly stale and irrelevant to the goal",
}}
SHORTLIST_QUESTION = {"type": "choice", "instructions": "Should this candidate file or input be included in the initial task shortlist? Keep uncertainty; prune only clearly unrelated candidates.", "criteria": {
    "keep": "Potentially relevant or uncertain; include it",
    "prune": "Clearly unrelated to the stated goal and acceptance criteria",
}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("Jev redirect forbidden")


def key_from_environment(root):
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    key_path = os.environ.get("TYPESAFE_API_KEY_FILE")
    if not key_path:
        raise FileNotFoundError("Set TYPESAFE_API_KEY_FILE to a protected file")
    path = Path(key_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name != "nt" and mode != 0o600:
        raise PermissionError("Jev key file must be mode 0600")
    return path.read_text().strip()


def local_router_key():
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "codex-router/generic-provider-credentials/jev.key"
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name != "nt" and mode != 0o600:
        raise PermissionError("Local Jev credential must be mode 0600")
    return path.read_text().strip()


def judgment(root, state, questions, cache_dir, transport=None):
    """Return typed answers and separate usage, or an unavailable marker."""
    payload = {"state": state, "model": MODEL, "questions": questions}
    digest = hashlib.sha256(json.dumps([RUBRIC_VERSION, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = Path(cache_dir) / ("jev-" + digest + ".json")
    if cache.exists():
        try:
            value = json.loads(cache.read_text())
            if value.get("model") == MODEL and isinstance(value.get("answers"), dict):
                return {**value, "cache_hit": True, "original_usage": value.get("usage"),
                        "usage": {"input_tokens": 0, "output_tokens": 0}, "usage_source": "local_cache"}
        except (OSError, ValueError):
            pass
    try:
        if transport is None:
            key = local_router_key()
            request = urllib.request.Request(LOCAL_URL, data=json.dumps({"state": state, "questions": questions}).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        else:
            key = key_from_environment(Path(root))
            request = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
            opener = transport
        response = None
        for attempt in range(2):
            try:
                with opener.open(request, timeout=3) as stream:
                    response = json.load(stream)
                break
            except urllib.error.HTTPError as error:
                if error.code in (502, 503, 504) and attempt == 0:
                    continue
                raise
        if not isinstance(response, dict) or response.get("model") != MODEL:
            raise ValueError("Unexpected Jev model or response")
        answers = response.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("Incomplete Jev answers")
        for name, question in questions.items():
            answer = answers[name]
            if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                raise ValueError("Invalid Jev answer type")
            if question["type"] == "choice":
                if answer.get("choice") not in question["criteria"]:
                    raise ValueError("Invalid Jev choice")
                confidence = answer.get("confidence")
                if not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
                    raise ValueError("Invalid Jev confidence")
        usage = response.get("usage")
        value = {"model": MODEL, "answers": answers, "usage": usage if isinstance(usage, dict) else None,
                 "usage_source": "typesafe_response" if isinstance(usage, dict) else "unknown", "cache_hit": False}
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with cache.open("x") as output:
            os.chmod(cache, 0o600)
            json.dump(value, output)
        return value
    except (OSError, ValueError, KeyError, urllib.error.URLError, TimeoutError) as error:
        return {"unavailable": type(error).__name__, "usage": None, "usage_source": "unknown", "cache_hit": False}


def task_classification(root, goal, acceptance, cache_dir):
    state = json.dumps({"goal": goal, "acceptance": acceptance}, ensure_ascii=False)
    return judgment(root, state, {"task_type": TASK_QUESTION}, cache_dir)


def should_prune(answer):
    item = answer.get("answers", {}).get("relevance", {})
    return item.get("choice") == "prune" and item.get("confidence", 0) >= 0.90


def choose_context(root, goal, candidates, cache_dir, question=None):
    """Batch optional excerpts under one state; uncertainty keeps every excerpt."""
    if not candidates:
        return {"kept": [], "pruned": [], "judgment": None}
    questions = {}
    for i in range(len(candidates)):
        rubric = dict(question or RELEVANCE_QUESTION)
        rubric["instructions"] = (f"Evaluate ONLY state.candidates[{i}].excerpt relative to state.goal. "
                                  + str(rubric["instructions"]))
        questions["candidate_" + str(i)] = rubric
    state = json.dumps({"goal": goal, "candidates": candidates}, ensure_ascii=False)
    result = judgment(root, state, questions, cache_dir)
    kept, pruned = [], []
    for i, candidate in enumerate(candidates):
        answer = result.get("answers", {}).get("candidate_" + str(i), {})
        text = candidate["excerpt"]
        protected = bool(re.search(r"\b(?:agents\.md|error|failed|diff|acceptance|instruction|ошибка|требование)\b|^@@", text, re.I | re.M))
        if not protected and answer.get("choice") == "prune" and answer.get("confidence", 0) >= 0.90:
            pruned.append(candidate["id"])
        else:
            kept.append(candidate)
    return {"kept": kept, "pruned": pruned, "judgment": result}
