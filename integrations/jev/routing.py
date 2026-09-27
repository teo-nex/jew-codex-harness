"""Jev judges task scope; code chooses the executing model."""

from .client import task_classification

LUNA = "gpt-6-luna"
LUNA_PREVIOUS = "gpt-5.6-luna"
TERRA_PREVIOUS = "gpt-5.6-terra"
ORCHESTRATOR = "gpt-6-sol"
MIN_LUNA_CONFIDENCE = 0.90


def choose_route(root, task, cache_dir):
    """Return a bounded route and the raw Jev judgment for audit."""
    pin = task.get("requested_model")
    if pin in (LUNA, LUNA_PREVIOUS, TERRA_PREVIOUS):
        return {"route": "quick" if task.get("question_only") else "luna", "model": pin,
                "effort": "low" if task.get("question_only") else "medium",
                "reason": "explicit_model_pin", "jev": None}
    if pin == ORCHESTRATOR:
        return {"route": "orchestrator", "model": ORCHESTRATOR,
                "reason": "explicit_model_pin", "jev": None}
    if task.get("deterministic"):
        return {"route": "orchestrator", "model": ORCHESTRATOR,
                "reason": "deterministic_local_action", "jev": None}

    result = task_classification(root, task["goal"], task["acceptance_commands"], cache_dir)
    answer = result.get("answers", {}).get("task_type", {})
    kind = answer.get("choice")
    confidence = answer.get("confidence", 0)
    common = {"category": kind, "confidence": confidence, "jev": result}
    if kind == "simple" and task.get("question_only") and confidence >= MIN_LUNA_CONFIDENCE:
        return {**common, "route": "quick", "model": LUNA,
                "effort": "low", "reason": "one_question"}
    if kind == "routine" and confidence >= MIN_LUNA_CONFIDENCE:
        return {**common, "route": "luna", "model": LUNA,
                "effort": "medium", "reason": "bounded_task"}
    if kind == "unclear" and confidence >= MIN_LUNA_CONFIDENCE:
        return {**common, "route": "orchestrator", "model": ORCHESTRATOR,
                "reason": "needs_clarification"}
    return {**common, "route": "orchestrator", "model": ORCHESTRATOR,
            "reason": "complex_or_uncertain" if "unavailable" not in result else "jev_unavailable"}
