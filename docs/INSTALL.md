# Agent installation guide

This is a source checkout, not a hosted service. Give this repository to any
coding agent and instruct it to read [AGENT_INSTALL.md](../AGENT_INSTALL.md),
then `AGENTS.md`. The bundled target adapter is Codex. First release targets
macOS, Linux and Windows **fresh Codex profiles**. Existing Codex Router profiles
require a separately reviewed migration; the installer refuses to take them
over. The router listens on loopback only.

## Required inputs

- Python 3.11+, Node.js 22.19+, npm, Codex CLI and a supported per-user service manager (`launchd`, `systemd --user`, or Windows Task Scheduler).
- A Jev decision key from TypeSafe or OpenRouter. `onboard` can read it from a hidden terminal prompt and write a protected file; non-interactive installs supply only the protected file **path**.
- For optional OmniRoute routing: an existing gateway auth JSON in a protected file with `omniroute.key`. The wizard can create your provider sequence; non-interactive installation supplies a private JSON based on `config/providers.example.json`. See [provider order and model mappings](PROVIDERS.md). Store verified connection IDs and model IDs outside Git. The legacy `config/ladder.example.json` remains supported.
- A Codex/ChatGPT login for native GPT routing. The account owner must complete
  OAuth and explicitly opt in to native session sharing after installation.

Interactive first install on an otherwise free host:

```sh
./install.sh onboard
```

On Windows, use `./install.ps1 onboard` in PowerShell. The wizard asks for a
fresh Codex profile, `typesafe` or `openrouter` for Jev, the hidden decision
API key, and whether to use an **existing** OmniRoute gateway. It never
installs OmniRoute or creates its accounts. Selecting OmniRoute requires
a protected gateway-auth file; choose `create` to enter your provider/account
order and exact model IDs, or `file` to reuse a protected configuration. A new
configuration is saved privately outside the repo before validated installation.
The key is written outside the repo
with private permissions; a failed preflight asks for no key. The wizard
reports local verification and the remaining hook-trust step separately.

Non-interactive native-only command order (use `py -3` in place of `python3` on Windows):

```sh
python3 -m harness.cli --jev-provider typesafe --jev-key-file /private/jev.key doctor
python3 -m harness.cli prepare
python3 -m harness.cli --jev-provider typesafe --jev-key-file /private/jev.key install --dry-run
python3 -m harness.cli --jev-provider typesafe --jev-key-file /private/jev.key install
python3 -m harness.cli verify
```

For OpenRouter Jev, use `--jev-provider openrouter` with a protected OpenRouter
key file. The server calls OpenRouter's [Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)
with the pinned `typesafe/jev-1.13` model. This account may incur API charges.

To enable the external ladder, add both `--ladder-config /private/ladder.json`
and `--omniroute-auth-file /private/omniroute-auth.json` to `doctor`, `install`,
and `resume`. Omitting both uses native Codex models through the local Jev router.
The choice is recorded in the installation manifest and cannot be changed by
re-running `install` against the same profile.

After `verify` passes local checks and the operator trusts the hooks, run `verify --live`. It checks a typed Jev decision through the selected decision provider, one synthetic, read-only `jev/auto` response, evidence that the Codex prompt hook loaded, and at least one priceable usage record for the cost report. It returns verdicts without raw event output and does not change the selected default model. It skips provider calls when local checks fail. Restarting an already open Codex app may be needed for the model picker. Real Browser/Computer/Compact acceptance remains a separate client capability check.

Codex requires review and trust of new non-managed hooks. Open a fresh Codex
session, use `/hooks` to review the installed definitions, then run the live
and manual-model Tool Use acceptance checks. Run
`python3 -m harness.cli verify --live --manual-model MODEL_ID` with a manual
model that this profile can actually serve; the tool check writes only one
synthetic file in an ephemeral workspace and requires a matching hook receipt.
File installation and service
health do not prove the client loaded a hook.

The installer bootstraps the router with credential discovery disabled. It leaves
native ChatGPT session sharing pending: local install and service health do not
prove that the Plus-first or native-only route can answer. After the account
owner signs in with `CODEX_HOME` set to the new profile, explicitly enable
discovery and sharing with `node router/src/discovery-mode.mjs set enabled` and
`node router/src/chatgpt-session.mjs enable`, using that same `CODEX_HOME` and
`CODEX_ROUTER_STATE_DIR`. This permits the router to read the Codex session;
review that access before opting in. Then run `verify --live` in the selected
profile. If OAuth changes `auth.json` during an interrupted installation,
inspect the journal and resume with `--reviewed-auth-sha256 HASH` for its exact
locally computed SHA-256. Do not print or send the auth file. A router bootstrap
that stopped before its ownership checkpoint remains blocked for manual review.

For an offline check of the real provider/model/key/catalog commands without touching the current profile or calling a model, run `python3 scripts/smoke_config_offline.py` (`py -3` on Windows).

After installation, local metrics are available through `./bin/jev-codex-router efficiency` and `./bin/jev-codex-router api-cost` on macOS/Linux, or the corresponding `integrations/jev/report_*.py` scripts on Windows. API cost is a list-price equivalent based on observed token counts; it is not a ChatGPT subscription debit or proof of equal model quality.

After the profile has served real calls, run the `report_command` returned by
`onboard`, or `./bin/jev-codex-router api-cost --summary` with `CODEX_HOME`
set to the installed profile. The table compares observed attempts with
same-token GPT-6 Sol and Astra API-price counterfactuals and shows missing
usage, unpriced attempts, and log-window coverage. Before any calls it says
there is no data; it never presents demo savings as observed. The private JSON
report can be rendered again with `--from-report PATH`, and the displayed
SHA-256 identifies those exact saved bytes.

`rollback` checks the journal, file hashes and service identity before changing anything. It stops the owned Jev service, removes unchanged harness files and restores any AGENTS.md text that preceded installation. It removes the base router service only when its exact definition proves ownership. The Codex profile and router state remain for inspection; a partial router bootstrap requires manual review. Keep the source checkout at a stable path while the service and hooks reference it.

The installer adds the global Jev Browser/Computer/Compact instructions to the profile's `AGENTS.md`. On macOS it also registers `jev-browser` when the CanvasTTY browser helper is installed; the browser broker remains optional elsewhere.

## Platform scope

| Component | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Local Jev router + provider ladder | offline tests, canary | code/tests only | code/tests only |
| Per-user background service | launchd adapter | systemd user adapter | Task Scheduler adapter |
| Global Codex hook/context/UI helper | packaged | packaged | packaged, ACL preflight |
| `jev-workers` | CanvasTTY path | read-only CLI worker | read-only CLI worker |
| CanvasTTY browser broker | optional | unavailable | unavailable |

Full live acceptance on Linux and Windows remains pending. The portable worker refuses mutable tasks until file ownership can be enforced there. No key, auth JSON, quota snapshot, router state or log belongs in this Git repository.
# Post-install route edits

On a complete OmniRoute-enabled owned profile, use
`python -m harness.cli --codex-home PROFILE routes show`, `routes check --config FILE`,
`routes apply --config FILE`, `routes reorder ID ID ...`, or `routes rollback`.
The protected JSON file uses the same schema as onboarding. Accounts and model
maps can be changed through `apply`. Every edit creates a private backup and
updates the installation journal. An interrupted transaction is recovered on
the next `routes` command. The running adapter reads the new config on its next
request; no service restart or credential modification occurs. Native-only
installations require a reviewed migration to enable an external gateway.

## Request diagnostics

`python -m harness.cli --codex-home PROFILE explain` reads bounded recent routing
evidence. `--scope HASH` filters by the logged request scope. It distinguishes
Jev selection, destination ID and provider-reported response model, includes
requested/effective reasoning, retry reasons and observed timings. Missing
model or timing evidence remains unknown. No prompt, output, tool arguments,
account credentials or raw log fields are exported. Provider-reported names
do not establish backend identity; reasoning enforcement remains unknown.
