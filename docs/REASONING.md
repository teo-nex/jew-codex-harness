# Reasoning Effort Routing

Jev already chooses model capability and reasoning depth independently for
`jev/auto`. This module maps that depth to an explicitly configured destination
contract on each provider-ladder attempt. Manual model selection and native-only
routing are unchanged. There is no additional model call for effort mapping.

## Configuration

Add a `reasoning_profiles` object to the existing protected ladder JSON passed
to the installer via `--ladder-config` (runtime `JEV_LADDER_CONFIG`).
Both the [ordered version 2 configuration](PROVIDERS.md) and legacy ladder
accept it; the interactive wizard can collect supported levels per destination.
Keys are exact destination IDs, not patterns. Example:

```json
{
  "reasoning_profiles": {
    "example/reasoning-model": {
      "supported_efforts": ["low", "high"],
      "effort_map": {"medium": "high", "max": "high"}
    },
    "example/model-without-effort-control": {"supported": false}
  }
}
```

The classifier chooses low/medium/high/xhigh/max. The mapping vocabulary also
includes none/minimal for explicitly configured destinations. Supported values
pass through first; an explicit map handles other values; otherwise the resolver
uses the lowest supported ceiling or the highest supported level. Disabling
thinking is never inferred from a model name. A profile with only none/minimal
must explicitly map every positive level. Malformed profiles fail validation.

Unconfigured models retain legacy effort and are labeled `unknown`.
`supported: false` removes effort while preserving other reasoning metadata.
Every fallback resolves the original requested depth again; a previous
provider's mapping cannot leak into the next attempt.

Attempts record `requested_effort`, `effective_effort`, `reasoning_status`
and `reasoning_source`, plus `selected_model` separately from the actual
destination `model`. These diagnostics are not sent as provider API fields.
Configured support is an operator assertion, not provider verification.
Keep effort-suffixed model aliases consistent with their configured profiles.

## Verification

`config/reasoning-profiles.example.json` is the synthetic probe configuration,
not an authoritative provider capability catalogue. Revalidate it for your
gateway/version. No built-in profile is installed automatically.

```sh
python3 scripts/smoke_reasoning_live.py --live \
  --auth-file /path/to/protected/auth.json \
  --profiles config/reasoning-profiles.example.json --tools
```

The smoke uses production resolution and payload shaping, then calls the local
gateway directly. It checks low/high JSON answers and a two-request synthetic
tool loop, at most four model requests per selected model, without provider fallback or service
changes. Keys stay in memory. The auth file may contain a raw key, a JSON
`key`/`token`, or `omniroute.key`. No network runs without `--live`.

Offline tests separately cover the real server ladder, including retries and
canonical input preservation. Live smoke is not a full Codex-client acceptance
test and cannot prove provider-internal effort enforcement, quality improvement
or speedup. See [the dated results](REASONING-ACCEPTANCE-2026-09-28.md).
