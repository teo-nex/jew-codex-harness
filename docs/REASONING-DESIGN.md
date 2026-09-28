# Reasoning Routing Design

## Existing Decision Layer

`server/routing_policy.py` already asks Jev independent questions for model
capability, reasoning depth and lease duration. This feature does not add
another classifier, heuristic keyword router or model request.

## New Capability Layer

1. `provider_ladder.validate_config` validates optional `reasoning_profiles`.
2. `Handler._serve_ladder` chooses a destination using the existing ladder.
3. `resolve_model_reasoning_effort` maps the original adaptive depth using
   that exact destination's operator-supplied profile.
4. Existing `apply_route_payload` writes Responses `reasoning.effort`.
5. The request, visible route marker and cache observations use the mapped
   depth. The lease retains the original adaptive decision.
6. Each attempt records requested/effective effort, status and mapping source.

The resolver accepts validated profiles. Supported values pass through first;
explicit mappings handle other levels before a ceiling/max projection.
Unconfigured destinations preserve compatibility and report unknown support.

## Invariants

- Retry resolution starts from the original requested depth, not the previous
  destination's mapped value.
- Automatic ladder requests discard stale flat thinking/effort fields.
- Unsupported effort control removes effort but preserves other reasoning
  metadata and canonical conversation input.
- Manual and native-only request paths are unchanged.
- Existing Astra configuration updates and replay behavior are reused.
- No user profile, service, model catalogue or gateway setting is changed.

## Evidence

`server/test_reasoning_effort.py` covers schema, matching and projection.
`server/test_ladder_reasoning.py` exercises the actual ladder handler with
isolated state and mock transport, including failure/retry telemetry.
Existing server regression tests cover leases, replay and Astra behavior.

`scripts/smoke_reasoning_live.py` reuses the resolver and payload shaper for
bounded real gateway calls. It does not exercise an installed Codex client or
observe provider-internal thinking controls. See the dated acceptance report
and [configuration guide](REASONING.md) for limitations.
