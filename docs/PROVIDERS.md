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
# Catalog selection

Onboarding reads the existing gateway's `/v1/models` catalog and selects model
numbers instead of inventing IDs. If the catalog is unavailable, exact-ID
entry remains available with an explicit unverified warning. Use
`python -m harness.cli --omniroute-auth-file FILE catalog` to inspect advertised
models, or add `--catalog` to `routes check` to flag absent destinations and
cross-family GPT aliases. Catalog advertising is not a successful generation
probe, backend identity proof, or a reasoning-enforcement guarantee.
# Project policies

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
