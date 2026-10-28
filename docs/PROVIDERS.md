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
  complete mapping from `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.1-sol` and
  `gpt-6-astra` to exact destination IDs. Do not supply both fields.
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
Older complete three-tier mappings remain valid without `gpt-6.1-terra`.
When Terra is selected, entries without its mapping are skipped; no bare
gateway ID or Sol destination is invented. Add an exact Terra destination or
an unpinned native entry to make Terra routable through that configuration.
Mappings may retain a `gpt-5.6-terra` key for compatibility;
validation ignores that retired entry.
Sol mapping keys must use `gpt-6.1-sol`; the retired `gpt-6-sol` key is rejected,
so an old gateway destination is never silently reused for the new model.
The legacy Plus adapter preserves the selected exact native model ID instead
of rewriting Luna 6 or Sol 6.1 to an older model alias. Effort remains a payload field.
Legacy ladders hand Terra 6.1 directly to the native transport, as they do Astra;
version 2 is required to configure an explicit external Terra mapping.

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
same request. For Codex `tool_search_output` history, an ordered OmniRoute route
may declare `"supports_tool_search_history": true` after its exact gateway/backend
path has been verified to preserve Responses tool discovery and subsequent tool
calls. The declaration defaults to false and is checked again on each fallback;
it is not inferred from provider or model names. Compatible routes receive the
canonical history unchanged. Otherwise, the request uses the first explicitly
configured native route without advancing the incompatible sticky provider.
Without such a native route, it fails clearly instead of inventing one. Legacy
ladder configurations retain the native-only handoff for this history.

For external routes, client-executed `tool_search` is declared on the wire as
an ordinary function with the same schema. Returned calls are restored to
native `tool_search_call` items for Codex. This keeps tool-only discovery
responses visible to gateway content checks. The canonical request and native
history remain unchanged; history support still requires the declaration above.
Ambiguous collisions with an ordinary function named `tool_search` fail locally
rather than reinterpreting that function. Genuine failure events after completion
remain errors and are never silently discarded.

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
`native_only` also bypasses the external Jev classifier, choosing native Sol
locally. Other policies restrict executing routes, not the configured Jev
classifier. `no_paid_fallback` excludes external routes with `billing: "paid"`
or unknown billing; `billing: "free"` and `"subscription"` are operator
declarations, not billing verification. Actual native subscription limits remain.

The opt-in `require_astra_final` override requires exact native `gpt-6-astra`
when Jev selects specialist/heavy work (`astra_policy: "astra"`) or the caller
sets `metadata.jev_phase: "final_review"`.
There is no cheaper fallback if that route is absent/unavailable. Semantic
classification is not deterministic proof that every specialist task was
detected; explicit phase metadata is the deterministic final-review trigger. Off/shadow
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
External SSE starts with a relay-owned lifecycle envelope and keepalives while
content and tool calls remain buffered for validation. The envelope consistently
reports the logical model `jev/auto`; the selected and resolved provider models
remain in the private attempt log, not in changing response snapshots.
Slow reasoning therefore
does not look like an upstream that never sent headers. Disconnecting the caller
interrupts the upstream socket instead of leaving generation running in the
background. The existing 180-second default is preserved; longer reasoning
turns need an explicit budget. Downstream writes are bounded by the remaining
total and idle limits, independently of the upstream watchdog. After exhaustion,
only the terminal failure envelope gets a bounded 250 ms drain; no generation or
retry is allowed during that drain. Exhausting the total/attempt budget
is terminal, not a retryable provider outage, and invalidates the route lease.
A bounded in-memory cache of request hashes rejects exact exhausted replays for
15 minutes, including retries issued by another proxy layer. No prompts are
stored. New turns/changed requests remain independent, and service restart
clears this cache.
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
