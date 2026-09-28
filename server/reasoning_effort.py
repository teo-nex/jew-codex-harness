"""Capability-aware reasoning effort routing.

Maps requested reasoning depth onto destination model capabilities without
altering canonical input or guessing unconfigured model features.
"""

from typing import Any, Dict, Optional, Tuple

CANONICAL_EFFORTS: Tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
EFFORT_RANK: Dict[str, int] = {effort: index for index, effort in enumerate(CANONICAL_EFFORTS)}
NONTHINKING_EFFORTS = {"none", "minimal"}


def validate_reasoning_profiles(profiles: Any) -> Dict[str, Any]:
    """Validate reasoning_profiles dictionary.

    Format:
    Keyed by EXACT model ID. Non-empty string keys, not whitespace only.
    Value must be:
      {"supported": False}
      OR
      {"supported_efforts": [...], "effort_map": {...}}
      where supported_efforts is a non-empty list of unique enum values from CANONICAL_EFFORTS,
      and effort_map (optional object, reject null/non-dict if provided) maps enum values
      from CANONICAL_EFFORTS to supported target values.

    Auto-mapping positive effort into nonthinking levels (none, minimal) is forbidden;
    requires explicit map for disabling.
    """
    if not isinstance(profiles, dict):
        raise ValueError("reasoning_profiles must be an object")

    validated: Dict[str, Any] = {}
    for model_id, profile in profiles.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("reasoning_profiles keys must be non-empty strings")
        if not isinstance(profile, dict):
            raise ValueError(f"reasoning_profile for {model_id!r} must be an object")

        allowed_keys = {"supported", "supported_efforts", "effort_map"}
        unknown_keys = set(profile.keys()) - allowed_keys
        if unknown_keys:
            raise ValueError(f"unknown keys in profile for {model_id!r}: {sorted(unknown_keys)}")

        if "supported" in profile:
            if profile["supported"] is not False:
                raise ValueError(f"profile for {model_id!r} with 'supported' must have supported=False")
            if "supported_efforts" in profile or "effort_map" in profile:
                raise ValueError(f"profile for {model_id!r} with supported=False cannot have supported_efforts or effort_map")
            validated[model_id] = {"supported": False}
            continue

        if "supported_efforts" not in profile:
            raise ValueError(f"profile for {model_id!r} must have supported_efforts or supported=False")

        efforts = profile["supported_efforts"]
        if not isinstance(efforts, list) or not efforts:
            raise ValueError(f"supported_efforts for {model_id!r} must be a non-empty list")

        seen = set()
        for effort in efforts:
            if not isinstance(effort, str) or effort not in CANONICAL_EFFORTS:
                raise ValueError(f"invalid effort {effort!r} in supported_efforts for {model_id!r}")
            if effort in seen:
                raise ValueError(f"duplicate effort {effort!r} in supported_efforts for {model_id!r}")
            seen.add(effort)

        if "effort_map" in profile and profile["effort_map"] is None:
            raise ValueError(f"effort_map for {model_id!r} cannot be null")
        effort_map = profile.get("effort_map")
        validated_map = {}
        if effort_map is not None:
            if not isinstance(effort_map, dict):
                raise ValueError(f"effort_map for {model_id!r} must be an object")
            for src, dst in effort_map.items():
                if not isinstance(src, str) or src not in CANONICAL_EFFORTS:
                    raise ValueError(f"invalid source effort {src!r} in effort_map for {model_id!r}")
                if not isinstance(dst, str) or dst not in CANONICAL_EFFORTS:
                    raise ValueError(f"invalid target effort {dst!r} in effort_map for {model_id!r}")
                if dst not in seen:
                    raise ValueError(f"target effort {dst!r} in effort_map not in supported_efforts for {model_id!r}")
                validated_map[src] = dst

        # If profile only contains nonthinking levels (none, minimal), positive requested efforts
        # must have explicit mapping in effort_map. If any positive effort is unmapped, reject
        # during validation so requests cannot 500 at runtime.
        thinking_levels = seen - NONTHINKING_EFFORTS
        if not thinking_levels:
            positive_efforts = set(CANONICAL_EFFORTS) - NONTHINKING_EFFORTS
            unmapped = positive_efforts - set(validated_map.keys())
            if unmapped:
                raise ValueError(
                    f"profile for {model_id!r} only supports nonthinking levels {sorted(seen)}; "
                    f"positive efforts {sorted(unmapped)} require an explicit effort_map"
                )

        validated[model_id] = {
            "supported_efforts": list(efforts),
            "effort_map": validated_map,
        }

    return validated


def resolve_model_reasoning_effort(
    model_id: str,
    requested_effort: Optional[str],
    profiles: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve requested reasoning effort depth for destination model.

    Supported values pass through. Otherwise use an explicit map, then
    the lowest supported ceiling or the highest supported level.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty non-whitespace string")

    if requested_effort is not None:
        if not isinstance(requested_effort, str) or requested_effort not in CANONICAL_EFFORTS:
            raise ValueError(f"invalid requested_effort: {requested_effort!r}")

    if profiles is None:
        profiles = {}

    profile = profiles.get(model_id)

    if profile is None:
        return {
            "requested_effort": requested_effort,
            "effective_effort": requested_effort,
            "status": "unknown",
            "source": "legacy_unknown",
        }

    if profile.get("supported") is False:
        return {
            "requested_effort": requested_effort,
            "effective_effort": None,
            "status": "unsupported",
            "source": "unsupported_profile",
        }

    if requested_effort is None:
        return {
            "requested_effort": None,
            "effective_effort": None,
            "status": "exact",
            "source": "exact",
        }

    supported_efforts = profile["supported_efforts"]
    effort_map = profile.get("effort_map", {})

    if requested_effort in supported_efforts:
        return {
            "requested_effort": requested_effort,
            "effective_effort": requested_effort,
            "status": "exact",
            "source": "exact",
        }

    if requested_effort in effort_map:
        return {
            "requested_effort": requested_effort,
            "effective_effort": effort_map[requested_effort],
            "status": "mapped",
            "source": "effort_map",
        }

    # Deterministic monotonic ladder mapping:
    # Prefer ceiling (lowest supported effort >= requested), then max (highest supported effort)
    req_rank = EFFORT_RANK[requested_effort]
    ceiling_candidates = [e for e in CANONICAL_EFFORTS if e in supported_efforts and EFFORT_RANK[e] >= req_rank]
    if ceiling_candidates:
        return {
            "requested_effort": requested_effort,
            "effective_effort": ceiling_candidates[0],
            "status": "mapped",
            "source": "ladder_ceiling",
        }

    max_candidates = [e for e in CANONICAL_EFFORTS if e in supported_efforts]
    return {
        "requested_effort": requested_effort,
        "effective_effort": max_candidates[-1],
        "status": "mapped",
        "source": "ladder_max",
    }
