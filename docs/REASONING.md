# Reasoning Effort Routing

Jev already chooses model capability and reasoning depth independently for
`jev/auto`. This module maps that depth to an explicitly configured destination
contract on each provider-ladder attempt. Manual model selection bypasses the
automatic capability policy; explicit provider/project restrictions still apply.
There is no additional model call for effort mapping.

The `jev/auto` Codex catalog entry omits the manual reasoning-effort selector:
Jev owns that choice. A saved client setting such as `medium` does not override
a normal automatic decision. Internal model metadata retains a compatibility
default, not a fixed execution depth. Routing-off and shadow modes preserve
client reasoning; classifier errors use the server's explicit fallback policy.
After updating the source, regenerate the managed catalog and open a fresh
Codex session to check the client presentation; an existing session may retain
its catalog or saved setting. Source and offline checks alone do not establish
desktop or terminal UI acceptance.

## Configuration

Automatic capability routing has three tiers: `gpt-6-luna` for a simple stable
question or small unambiguous edit, `gpt-6.1-sol` for ordinary work, and
`gpt-6-astra` only for 3D/spatial work, computer use or heavy software
development. Ordinary architecture questions, security reviews and final code
reviews do not by themselves require Astra. A model-only Astra answer outside
that scope is constrained to Sol. Uncertain Luna decisions still promote to
Sol/high; effort otherwise remains an independent Jev choice on each decision.
Classifier failures, routing-off/shadow baselines and native-only project
transport default to Sol. An operator's explicit `require_astra_final` policy
and manual model choices remain overrides, not automatic classifications.
Sol always means `gpt-6.1-sol` in automatic routing; `gpt-6-sol` is retired.
Historical cost reports retain their exact old model IDs and price cards;
Sol 6.1 usage is counted as unpriced until its own rate card is verified.

Route reuse requires a real `prompt_cache_key`, an unchanged user turn and
request contract, and a clean tool continuation. A lease lasts at most five
minutes and eight reuses; successful calls do not renew those limits. Changed
turns, errors, exhausted leases and tool-chain changes trigger reassessment.
In-memory route and cache scopes are capped at 256 each and expired sessions
are collected. Provider selection still applies when no cache key is present;
only continuity is disabled in that case.

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
