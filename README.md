# Jev Agent-First Install Kit

Give this repository to a coding agent and ask it to follow
[`AGENT_INSTALL.md`](AGENT_INSTALL.md). The agent's own harness can be Codex,
Claude Code, Gemini CLI, Hermes or another environment; **the only target-client
adapter included today is Codex**. Instructions for an agent running in another
harness do not install Codex hooks into that harness. See the
[adapter contract](docs/ADAPTER-CONTRACT.md) before adding another target.

This is a **public alpha**. Automatic installation currently supports a fresh
Codex profile on macOS, Linux and Windows. If the intended profile or router is
already in use, stop at preflight and report `BLOCKED_MIGRATION`; do not switch
profiles or replace services to make installation pass. The installer preserves
the selected default model and does not overwrite an existing profile.

## Give the repository to an agent

Send the agent this repository and the following task:

> Read `AGENT_INSTALL.md` and `AGENTS.md`. Check whether this host has a fresh
> Codex profile and free, correctly owned service ports before changing anything.
> If an existing profile or router would need migration, stop and report the
> exact blocker; do not choose a different profile for me. For a fresh profile,
> use the interactive `onboard` flow and enter the Jev key only in its hidden
> local prompt. Ask whether to use my existing OmniRoute gateway; never install
> OmniRoute or invent credentials. Then report what the installer verified and
> guide me through the owner-only steps: review hooks, sign in and explicitly
> enable native session sharing if needed, select `jev/auto` if I want autorouting,
> and run the live checks. Do not claim a turnkey install from CI results alone.

The intended flow is:

1. **Preflight:** identify the target client/profile, check ports, services,
   disk space and prerequisites. Existing-profile migration is unsupported.
2. **Choose routing:** `onboard` asks for TypeSafe or OpenRouter as the Jev
   decision provider, then whether to use an already-configured OmniRoute
   gateway. OmniRoute is optional; the installer does not install it.
3. **Enter the Jev key locally:** the wizard checks for structural blockers
   before showing a hidden terminal prompt. It stores the key in a protected
   file outside the repository. Never paste credentials into chat or Git.
4. **Install and verify locally:** `onboard` runs preparation, offline smoke,
   install dry-run, installation, local file/service checks and a typed Jev
   decision. This still does not prove that a fresh Codex session loaded hooks
   or completed a real `jev/auto` task.
5. **Owner actions and live check:** review and trust hooks with `/hooks` in a
   fresh Codex session. Sign in and explicitly opt in to native session sharing
   if using native GPT routing. Select `jev/auto` if you want autorouting, then
   run `verify --live --manual-model MODEL_ID` with a model available to that
   profile. Report skipped Browser/Computer/Compact checks as untested.

## Run the interactive installer yourself

Clone the repository, then run the command for your shell from its root:

```sh
# macOS / Linux
git clone https://github.com/teo-nex/jew-codex-harness.git
cd jew-codex-harness
./install.sh onboard
```

```powershell
# Windows PowerShell
git clone https://github.com/teo-nex/jew-codex-harness.git
Set-Location jew-codex-harness
.\install.ps1 onboard
```

The wizard requires a **new Codex profile** and asks for the Jev provider, key,
and optional existing OmniRoute files. It does not create provider accounts or
install OmniRoute. See the full [installation guide](docs/INSTALL.md) for
prerequisites, protected-file setup, non-interactive commands and rollback.

## What works, and what remains unproven

The [GitHub Actions CI](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml)
includes synthetic-key fresh-profile install smoke on macOS, Linux and Windows.
Passing CI proves the tested
offline install path and owned-service rollback on hosted runners; the synthetic
key cannot authenticate to a provider. It does **not** prove a subscriber's
first install, Codex OAuth, hook trust in a fresh interactive client, live model
quality, or interactive worker windows. See the
[support matrix](docs/SUPPORT.md) and [CI scope](docs/CI.md).

CanvasTTY is optional integration, not an installer target. On macOS, the
CanvasTTY browser broker is registered only when its helper is available. The
native `jev-workers` path is CanvasTTY-specific; Linux and Windows currently
expose read-only CLI worker tasks, and the CanvasTTY browser broker is
unavailable there. This kit does not install CanvasTTY or create visible
CanvasTTY terminal windows.

The root project is licensed under [MIT](LICENSE). The embedded router retains
its own [MIT license](router/LICENSE) and attribution in [`router/NOTICE.md`](router/NOTICE.md).
Credential values, OAuth files, provider state and runtime logs belong outside
Git; the CI repository-hygiene check rejects tracked secret paths and runtime
artifacts.

After real work, use the installer's `report_command` to produce its
24-hour API-price comparison. It is a same-token price estimate, not money paid,
subscription savings or proof of equal task quality.

The installer preserves the current default model. Selecting `jev/auto` enables
the provider ladder. Existing live profiles require a separately verified
migration; this installer stops without changing them. See
[source provenance](ROUTER_FORK.md).

Optional [reasoning profiles](docs/REASONING.md) map Jev's selected effort to
each destination model during automatic provider fallback. Unknown models keep
legacy behavior; per-attempt diagnostics distinguish requested and mapped depth.
