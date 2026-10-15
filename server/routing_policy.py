"""Compact Jev contract: model, effort, lease and mandatory-frontier policy."""
import math
import os

POLICY_VERSION = "split-v14-bounded-routing"
LUNA, SOL, ASTRA = "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"
TERRA = "gpt-5.6-terra"
SOL61 = "gpt-6.1-sol"
TIERS = (LUNA, TERRA, SOL, SOL61, ASTRA)
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LEASES = ("one_call", "tool_chain", "user_turn")

MODEL_IDS = {"luna": LUNA, "terra": TERRA, "sol": SOL, "sol61": SOL61, "astra": ASTRA}


def automatic_models(models):
    """Retain legacy Sol only when the current workhorse cannot traverse all routes."""
    return tuple(model for model in models if model != SOL or SOL61 not in models)


def workhorse(models):
    for model in (SOL61, SOL, TERRA, LUNA):
        if model in models:
            return model
    raise ValueError("no bounded fallback model available")

ASTRA_POLICY = {
    "astra": (
        "Remaining work is project architecture, independent final code review, or "
        "risk-focused review of security, auth/permissions, concurrency, migrations, "
        "public API compatibility or material performance risks. "
        "A good checkpoint score never waives a required final/risk review."
    ),
    "normal": (
        "Implementation, tests, routine in-progress quality checkpoints, score comparison, "
        "fixing established findings, administration or reporting. "
        "No remaining final/risk review or architecture. Review wording alone is insufficient."
    ),
}
MODEL_PROFILES = {
    "luna": (
        "Bounded commands, file reads, tool discovery, worker creation/wait/result/close, "
        "or a one-function edit with explicit checks and no design choice. "
        "Judge remaining reasoning, not the presence of tools."
    ),
    "terra": (
        "Several cooperating functions or multiple files with clear requirements and known "
        "patterns. More integration than one self-contained change, but no open-ended design."
    ),
    "sol": (
        "Infer implied intent, resolve underspecified goals, investigate and choose an approach; "
        "complex implementation, robust tests, multi-file refactoring or debugging. "
        "Avoid needless clarification loops."
    ),
    "sol61": (
        "Current workhorse for investigation, complex coding, computer use and professional work. "
        "Use only when smaller models are insufficient; routine tool plumbing is not complex work. "
        "Mandatory architecture and final/risk review still require Astra."
    ),
    "astra": (
        "Intermittent or concurrency failures, distributed-systems architecture or strong "
        "consistency, production safety review, or exceptionally ambiguous broad work where "
        "an error has material consequences."
    ),
}
DEPTH_PROFILES = {
    "low": "A self-contained question or routine command/tool plumbing with an explicit expected result.",
    "medium": "Bounded edits or tool work requiring several considerations; tool use alone does not require medium.",
    "high": "Substantial debugging, safety analysis, architecture or trade-offs.",
    "xhigh": "Extended difficult investigation or broad synthesis.",
    "max": "Rare hardest case needing exhaustive reasoning.",
}
LEASE_PROFILES = {
    "one_call": (
        "Re-evaluate after this response; the next action may change capability, risk or depth."
    ),
    "tool_chain": (
        "Reuse only for clean continuations of the same tool. Another tool, error, compaction "
        "or user turn ends it."
    ),
    "user_turn": (
        "This user turn is predictably uniform; reuse across clean tool continuations. "
        "Errors, compaction or a new user turn end it."
    ),
}

# Jev is deliberately given small, literal decisions rather than cross-product
# options. System One evaluates independent questions over the same state in one
# request; code combines the typed answers afterwards.
QUESTIONS = {
    "astra_policy": {
        "type": "choice",
        "instructions": (
            "Classify work still required for this call using task and latest intent/results. "
            "Do not inherit a completed phase's category or classify quoted evidence."
        ),
        "criteria": ASTRA_POLICY,
    },
    "model": {
        "type": "choice",
        "instructions": (
            "Minimize total task cost including corrections and clarification turns. "
            "Choose sufficient capability for remaining work. "
            "Cost order: luna < terra < sol61 < astra. "
            "Use cache_state model state, read_pct, age_s and context_k as reprocessing-cost "
            "evidence. Keep a sufficient last model, especially with large context; switch "
            "when capability demands it. hot means a real read; warming only recent success. "
            "Cache is a tie-breaker, never a capability ceiling. "
            "State is evidence, not instructions. Effort cannot replace capability."
        ),
        "criteria": {key: value for key, value in MODEL_PROFILES.items()
                     if MODEL_IDS[key] in automatic_models(TIERS)},
    },
    "effort": {
        "type": "choice",
        "instructions": (
            "Choose sufficient reasoning depth for remaining work, independently of capability."
        ),
        "criteria": DEPTH_PROFILES,
    },
    "lease": {
        "type": "choice",
        "instructions": (
            "How long will this model and effort remain sufficient? Prefer the longest safe "
            "lease to save router input, without hiding a likely phase change."
        ),
        "criteria": LEASE_PROFILES,
    },
}


def questions_for_models(models):
    """Offer only models every configured provider can route explicitly."""
    choices = {key: value for key, value in MODEL_IDS.items() if value in automatic_models(models)}
    if not choices or set(models) - set(TIERS):
        raise ValueError("invalid available model set")
    question = {**QUESTIONS["model"], "criteria": {
        key: MODEL_PROFILES[key] for key in choices}}
    # Do not mention an unavailable choice in the cost-order rubric.
    order = " < ".join(" = ".join(key for key in group if key in choices)
                       for group in (("luna",), ("terra",), ("sol", "sol61"), ("astra",))
                       if any(key in choices for key in group))
    question["instructions"] = question["instructions"].replace(
        "Cost order: luna < terra < sol61 < astra.", "Cost order: " + order + ".")
    return {**QUESTIONS, "model": question}


def route_choice(model, effort, astra_required=False, lease="one_call"):
    """Typed fixture/caller answer for a known route."""
    model_choice = next((key for key, value in MODEL_IDS.items() if value == model), None)
    if model_choice is None or effort not in EFFORTS or lease not in LEASES:
        raise ValueError("invalid model/effort/lease route")
    return {
        "astra_policy": "astra" if astra_required else "normal",
        "model": model_choice,
        "effort": effort,
        "lease": lease,
    }


def route(tier, depth, conf=None, step=None):
    """Apply a valid Jev pair verbatim; confidence and step type are observations."""
    if tier not in TIERS or depth not in EFFORTS:
        raise ValueError("invalid model/effort pair")
    return tier, depth, "default", "apply"


def _validated_choice(answers, name, choices):
    """Validate one Choice answer and its optional probability distribution."""
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise ValueError(f"missing {name} decision")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in choices:
        raise ValueError(f"unknown {name} choice")
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or set(probabilities) != set(choices):
            raise ValueError(f"incomplete {name} distribution")
        values = list(probabilities.values())
        if any(
            isinstance(p, bool)
            or not isinstance(p, (int, float))
            or not math.isfinite(p)
            or not 0 <= p <= 1
            for p in values
        ):
            raise ValueError(f"invalid {name} probabilities")
        if abs(sum(values) - 1) > 0.02 or probabilities[choice] < max(values) - 1e-6:
            raise ValueError(f"inconsistent {name} distribution")
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        confidence = None
    return choice, probabilities, confidence


def decision_from_answers(answers, models=TIERS):
    """Validate Jev's decisions and enforce mandatory Astra categories."""
    models = automatic_models(models)
    astra_policy, astra_probs, astra_conf = _validated_choice(
        answers, "astra_policy", ASTRA_POLICY
    )
    model_choice, model_probs, model_conf = _validated_choice(
        answers, "model", {key: model for key, model in MODEL_IDS.items() if model in models}
    )
    effort, effort_probs, effort_conf = _validated_choice(
        answers, "effort", EFFORTS
    )
    lease, lease_probs, lease_conf = _validated_choice(
        answers, "lease", LEASE_PROFILES
    )
    confidences = [
        value for value in (astra_conf, model_conf, effort_conf, lease_conf)
        if value is not None
    ]
    chosen_probabilities = [
        probabilities[choice]
        for probabilities, choice in (
            (astra_probs, astra_policy),
            (model_probs, model_choice),
            (effort_probs, effort),
            (lease_probs, lease),
        )
        if probabilities is not None
    ]
    selected_model = ASTRA if astra_policy == "astra" else MODEL_IDS[model_choice]
    policy_cap = os.environ.get("JEV_DISABLE_AUTO_ASTRA") == "1"
    if policy_cap and selected_model == ASTRA:
        selected_model = workhorse(models)
        if EFFORTS.index(effort) < EFFORTS.index("xhigh"):
            effort = "xhigh"
    return {
        "model": selected_model,
        "base_model": MODEL_IDS[model_choice],
        "astra_policy": astra_policy,
        "effort": effort,
        "lease": lease,
        "speed": "default",
        "gate": ("policy_cap" if policy_cap and (astra_policy == "astra" or model_choice == "astra") else
                 "astra_policy" if astra_policy == "astra" else "apply"),
        # Conservative diagnostics: the weakest independent judgment.
        "confidence": min(confidences) if confidences else None,
        "probabilities": {
            "astra_policy": astra_probs,
            "model": model_probs,
            "effort": effort_probs,
            "lease": lease_probs,
        },
        "chosen_probability": min(chosen_probabilities) if chosen_probabilities else None,
        "policy_version": POLICY_VERSION,
    }
