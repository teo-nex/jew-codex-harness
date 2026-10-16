# User-owned provider order

The interactive installer (`./install.sh onboard`, or `./install.ps1 onboard`)
can create a protected provider configuration when you select an existing
OmniRoute gateway. Choose `create`, then enter the routes in your preferred
order. You choose the transport, exact model IDs, optional connection IDs and
declared reasoning levels. Nothing requires Plus, Gemini, seven accounts, or a
native fallback. The wizard does not install OmniRoute or create its accounts.

For non-interactive installation, adapt
[`config/providers.example.json`](../config/providers.example.json), store it
outside Git with mode 0600 (private ACL on Windows), and pass its path through
`--ladder-config` together with `--omniroute-auth-file`. The installer validates
and copies this exact sequence into its owned private profile state.

## Version 2 contract

`providers` is an ordered array of 1-32 entries. IDs must be unique lowercase
identifiers; they are labels, not predefined provider names. Each entry has:

- `id`: your route label.
- `transport`: `omniroute` (default) or `native` (the bundled Codex router).
- `model`: one exact destination ID for every Jev decision; or `models`: a
  complete mapping from `gpt-6-luna`, `gpt-5.6-terra`, `gpt-6-sol` and
  `gpt-6-astra` to exact destination IDs. It may additionally map `gpt-6.1-sol`;
  existing four-model mappings remain valid. Do not supply both fields.
- `connection_ids`: optional ordered list of 1-32 unique OmniRoute account IDs.
  Omitting it lets the existing gateway choose the connection. Native routes
  cannot specify external account IDs.

A native entry with neither `model` nor `models` preserves Jev's selected native
model. A fixed model deliberately replaces that selection. A mapping makes
every replacement explicit; model aliases are not rewritten automatically.
Use verified gateway model IDs, preferably base IDs rather than effort-suffixed
aliases. Selecting a different model manually in Codex bypasses this sequence.

`reasoning_profiles` uses the same exact destination IDs, including models in
the mapping. See [reasoning routing](REASONING.md). Unknown support is recorded
as unknown, not inferred from a provider name. An empty profile object is valid.
Mixed legacy/v2 fields, duplicate IDs, partial model mappings and invalid
reasoning profiles are rejected before installation.

## Optional model tiers

When GPT-6.1 Sol is available across the provider sequence, automatic routing
offers it instead of GPT-6 Sol. Manual model IDs and legacy mappings remain valid. Add
`"gpt-6.1-sol": "codex/gpt-6.1-sol"` to each mapped provider that has verified
this exact destination. A choice is offered only when every mapped fallback
provider includes it; fixed routes still replace any choice explicitly, and
unmapped native routes preserve it. Legacy sequences keep their original four
choices. The wizard retains its four required destinations; optional mappings
can be added to the protected provider file before installation or reconfiguration.

For external Responses requests the harness forwards the caller's Codex version
when present, otherwise detects the installed CLI through the bundled router's
shim-safe resolver (cached for five minutes). Explicit CLI configuration takes
priority, then a real CLI on PATH or in the user's local bin, then bundled desktop
copies. Router shims are never executed. It never fabricates a newer version.
This matters when a gateway's default client version predates a newly added model.
The verified GPT-6.1 Sol reasoning levels are low, medium, high, xhigh and max:
[official model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
Gateway adapters can narrow that range; declare their observed limits in
`reasoning_profiles` rather than assuming the entire published range works on
every path. Its API rate is included in reports; native credit rates remain unknown until
independently verified. Routine commands and orchestration bookkeeping can use Luna
with low effort. A confidence score alone never upgrades a valid small-model decision.
A technical decision failure falls back to Sol 6.1 (or Sol 6 on legacy-only routes),
medium effort; the exception type is recorded without private error text. On failure,
`jev_error_stage` and `jev_error_reason` identify the transport, response parsing,
response validation, decision validation, or route application phase. Known
validation failures also record `jev_error_field` (one of the four question
names); HTTP refusals record a numeric `jev_error_http_status`. These enum-only
fields are available in `explain` and in both native and provider-ladder logs,
including failed fallback attempts. Unknown exceptions use `unexpected_error`.
Exception messages, tracebacks, provider bodies, keys and prompt text are never
copied into these diagnostics. The legacy `jev_error:<type>` gate format and
bounded fallback policy remain unchanged. Existing type-only log entries cannot be reconstructed.
Mandatory Astra architecture and final/risk reviews stay intact.

## Fallback and recovery

Failures before output exposure try account IDs, then providers, in the supplied
order. The working account/provider stays selected for that thread across
requests and server restarts. No attempt can advance a newer route with a stale
failure receipt. Exhausting the primary opens a shared cooldown; parked threads
probe it again after the reset time, retaining their previous fallback if the
probe fails. Without a usable reset header, cooldowns are bounded: normally 30
seconds, 300 for quota failures, and 900 for HTTP 400/401/403.
Recovery probes happen only at the start of a new request; slow fallbacks cannot
restart the sequence partway through the same request.

Changing the validated configuration invalidates old thread selections and
cooldowns instead of interpreting an old index against a new order. This is a
runtime state safeguard, not permission to bypass the install manifest or adopt
an existing profile. Corrupt recovery state fails closed with a controlled error.

An incomplete response already exposed to the client is never replayed in the
same request. A Codex `tool_search_output` history cannot be represented by this
OmniRoute adapter: it uses the first explicitly configured native route for
that request only, without changing the sticky provider. Without such a route,
the request fails clearly instead of silently inventing one.

## Legacy configuration

[`config/ladder.example.json`](../config/ladder.example.json) remains supported
for existing installations. It retains its fixed Plus/Gemini/optional
Opus/GLM/DeepSeek/optional Wally/native order. Legacy Plus maps Luna and Sol to
the historical GPT-5.6 gateway aliases, with the requested effort in the alias;
this is compatibility behavior, not proof of a GPT-6 upstream model. Astra now
stays on exact native Astra rather than silently becoming Sol. Use version 2
for an installation-specific sequence and explicit model replacements.

Attempts record `selected_model` separately from the destination `model`, plus
the requested/effective reasoning levels. These are routing evidence, not proof
of upstream model identity or provider-internal effort enforcement.
## Catalog selection

Onboarding reads the existing gateway's `/v1/models` catalog and selects model
numbers instead of inventing IDs. If the catalog is unavailable, exact-ID
entry remains available with an explicit unverified warning. Use
`python -m harness.cli --omniroute-auth-file FILE catalog` to inspect advertised
models, or add `--catalog` to `routes check` to flag absent destinations and
cross-family GPT aliases. Catalog advertising is not a successful generation
probe, backend identity proof, or a reasoning-enforcement guarantee.
## Project policies

Version 2 accepts `project_policies`, keyed by absolute project root, and
`project_scopes`, mapping the 16-character `cache_scope` hash from `explain` to
one of those roots. Edit the protected config using `routes apply`; bind a new
session using `routes bind --scope HASH --project PATH`. Prompt text and
request-supplied paths cannot authorize a different policy. With policies
configured, an unbound session is native-only until explicitly bound.

Policy switches: `native_only`, `no_paid_fallback`, `require_astra_final`, plus
`allowed_providers` (external sequence IDs; native transport remains allowed).
`native_only` also bypasses the external Jev classifier, choosing native Astra
locally. Other policies restrict executing routes, not the configured Jev
classifier. `no_paid_fallback` excludes external routes with `billing: "paid"`
or unknown billing; `billing: "free"` and `"subscription"` are operator
declarations, not billing verification. Actual native subscription limits remain.

`require_astra_final` requires exact native `gpt-6-astra` when Jev classifies
mandatory frontier work or the caller sets `metadata.jev_phase: "final_review"`.
There is no cheaper fallback if that route is absent/unavailable. Semantic
recognition by Jev is not deterministic proof that every final review was
detected; explicit phase metadata is the deterministic trigger. Off/shadow
flags cannot bypass configured project restrictions. These policies apply to
`jev/auto`, not manually selected models in the parent client.
## Request budgets

Optional `request_budget` (version 2 and legacy) sets `connect_seconds` (15),
`first_token_seconds` (60), `idle_seconds` (30), `total_seconds` (180), and
`max_attempts` (8). Seconds are finite 0.05-900, attempts 1-1024. The adapter's
defaults also apply without an external ladder. The monotonic total deadline
includes classification time and all relay attempts; it never resets on retry.
The existing Jev classifier has its own bounded 4-second call (capped by the
remaining budget). Relay socket waits are interrupted even for an active stream.
SSE lifecycle heartbeats do not count as a first token. For non-stream JSON,
first bytes are the observable boundary; token timing remains unknown.

Timeouts before visible output are retryable within the same remaining budget.
After a visible native stream/tool event, the relay ends the stream and never
replays that call. `explain` reports timeout phase and per-attempt timing.
## Failure classes and recovery

Ordered routes classify bad payload (400/413/422), auth (401/403), quota
(402/429), explicit rate limit, missing model (404), timeout, unavailable
transport and invalid output. A bad payload stops the call without advancing
or cooling a healthy account. Client disconnect and total/attempt budget
exhaustion also do not poison provider health. Invalid output is retried only
before anything is visible, as with other relay failures.

Auth cooldown defaults to 900 seconds, quota/model to 300, explicit rate limit
and transient failure to 30. Advertised reset time is accepted only when finite,
future and at most seven days away. Cooldowns persist per configuration and
hashed account (or route/model when no account); a missing model blocks that
destination across its accounts. New sessions skip unhealthy slots; another
account and unrelated routes remain usable. Complete exhaustion can recover a
secondary route before a longer primary auth cooldown. Legacy configs retain
their historical sticky/shared-primary state and request-shape 400 handoffs; convert using `routes apply` for
full per-account cooldowns. No upstream error body enters routing telemetry.


### Fixed primary model with automatic workers

A client route whose upstream model is one of the configured native model IDs
(for example `gpt-6.1-sol`) requests that exact model and reasoning effort from
the Jev endpoint. It skips classification and route leases, while retaining the
provider sequence and its transport checks. Unsupported fixed models or effort
values fail without substituting another model. Project routing policy still
applies. The default effort for a fixed request is `medium`.

Keep `jev/auto` as the worker route; its upstream model remains `auto` and each
worker keeps its independent automatic decisions. In login-free Codex profiles,
check the router's native alias map before selecting a native-looking model ID:
a picker slot may refer to `jev/auto`. Declare a distinct curated fixed route
and publish the catalog before making that route the primary session default.
The installer continues to preserve the person's selected default model.
