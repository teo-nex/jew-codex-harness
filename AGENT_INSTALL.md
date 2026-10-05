# Agent-first installation protocol

This file is the entry point for **the agent doing the installation**. That
agent may run in any coding harness. The only complete **target client** adapter
bundled here is Codex. Read `AGENTS.md` for its concrete commands and
`docs/ADAPTER-CONTRACT.md` before building an adapter for another target.

## 1. Discover before changing anything

Identify the user's intended target client, operating system, current profile,
active processes, service/port owners, installed hooks/MCP tools, selected
model, available provider routes and local credential **paths**. Read the
client's own extension documentation when its interface is unfamiliar. Do not
print credential values, OAuth files, prompts, or full local configuration.

Record whether the target is a fresh profile or an existing installation. A
different `CODEX_HOME` on the same host is **not** a clean-host test when router
ports or per-user services are shared. Existing Codex Router services must not
be stopped or replaced merely to make preflight pass.

Check free disk space before `prepare`: the embedded router installs Node
packages and a hash-pinned Python dependency set that includes LiteLLM. If
the host cannot accommodate that preparation, return `BLOCKED_RESOURCES`
without touching the live Codex profile. A partially prepared checkout is not
an installed client; remove only its identified, owned temporary artifacts.

## 2. Choose the target adapter and scope

| Target client | Included route | What the agent may claim |
| --- | --- | --- |
| Codex, fresh profile | `AGENTS.md` and `docs/INSTALL.md` | Install after preflight; claim working only after client and live checks below. |
| Codex, existing profile/router | Read-only inventory and reviewed migration plan | The current installer intentionally refuses takeover. Report `BLOCKED_MIGRATION` until a separate scoped migration is implemented and verified. |
| Another client | `docs/ADAPTER-CONTRACT.md` | Implement a client-specific adapter with tests if the user requested this target. Report capabilities without an adapter as `UNSUPPORTED`, never as installed. |

The Jev decision service can recommend a model, capability or UI element. The
target client still owns permissions, execution, retries, recovery and final
output. A generic MCP registration alone does not prove global tool control,
browser actions, context compaction or model routing in that client.

## 3. Install the supported Codex adapter

On a fresh host/profile, run `doctor` before asking for credentials. For an
interactive setup, use `./install.sh onboard` on macOS/Linux or
`./install.ps1 onboard` on Windows. The wizard collects a TypeSafe or OpenRouter
Jev key in a local hidden prompt and optionally asks for an **existing** protected
OmniRoute gateway-auth file. It can create a private provider configuration from
the user's chosen order and model IDs, or reuse an existing protected file.
It does not install OmniRoute or create
provider accounts. For non-interactive work, use protected key **file paths**
and the exact `doctor → prepare → install --dry-run → install → verify` sequence
in `docs/INSTALL.md`. Read installed CLI `--help` before changing syntax.

`onboard` is the interactive path: it asks for the new Codex profile and routing
choice, checks structural blockers before collecting the Jev key, then prepares,
dry-runs, installs and performs local checks plus one typed Jev decision. If
preflight finds an existing profile/router conflict, stop and report
`BLOCKED_MIGRATION`; do not select another profile or replace a service without
the owner's direction. The key prompt is local and hidden. For OmniRoute, ask
for the auth path to an already-configured gateway and let the user choose the
provider/account order and exact model IDs, or supply an existing protected
configuration. Do not hardcode the installer's author-specific provider order;
follow `docs/PROVIDERS.md`. Never ask for gateway credentials in chat.

The installer preserves the chosen default Codex model. Autorouting applies
only when `jev/auto` is selected. A manually selected model keeps ordinary
Codex Tool Use; Jev UI helpers and compact carry remain separate. Do not claim
that every new session autoroutes unless the installed profile explicitly
selects `jev/auto` and a fresh session proves it.

If the user asks for an existing-profile migration, first produce an exact
owned-file/service diff and a reversible migration plan. Do not use a new
profile, a second write mechanism, or another model call to bypass a hook
denial or an occupied service. Continue with checks that do not depend on the
blocked installation.

## 4. Prove the result in the target client

Keep these evidence levels separate:

1. **Source and files:** exact commit/artifact, protected credential paths,
   valid config and owned file changes; offline tests pass.
2. **Service:** the intended instance owns the expected loopback port and
   responds to a typed Jev decision. HTTP health alone stops here.
3. **Client loading:** a fresh client process loads the installed hooks/MCP
   configuration. For Codex, review and trust new hooks with `/hooks`.
4. **Live behavior:** one completed `jev/auto` response with model/route
   evidence, plus a small manual-model tool edit that is not vetoed by Jev.
   Check Browser Use, Computer Use and compact carry in the actual client only
   if those capabilities exist there. Prove a denied risky action separately.

Run `verify --live --manual-model MODEL_ID` where the model is actually
available to that profile. Record `PASS`, `FAIL`, `BLOCKED` or
`NOT_SUPPORTED` for each capability; never convert a skipped check into a
pass. Keep generated logs and reports outside Git. The API-cost report is a
same-token price comparison for observed `jev/auto` attempts, not a charge to
the user's subscription or evidence of equal task quality. Exclude synthetic
smoke calls from any public savings claim.

After installation, the account owner must review and trust the hooks with
`/hooks` in a fresh Codex session. Native GPT routing also requires the owner to
sign in and explicitly enable native session sharing for this profile. The
installer preserves the selected default model; autorouting is active only
after `jev/auto` is selected. CanvasTTY is optional: the browser broker is
available only when its macOS helper is present; native visible worker windows
are not part of the cross-platform installer. Linux and Windows worker support
is read-only CLI only, and the CanvasTTY browser broker is unavailable there.

## 5. Return one short installation report

Include the target client and profile, source commit, selected default model,
Jev provider mode, OmniRoute mode, owned changes, each evidence level above,
tests run, exact blockers, and the next user action if OAuth or hook trust is
required. If an owned install fails, use only its journal-aware `rollback` or
`resume` path. Preserve unrelated profiles, terminals, services and files.

The outcome must be one of `VERIFIED`, `PARTIAL`, `BLOCKED_MIGRATION`,
`BLOCKED_RESOURCES`, or `UNSUPPORTED_TARGET`. `VERIFIED` requires a real
completed response through the intended client, not only a successful installer
exit code.
