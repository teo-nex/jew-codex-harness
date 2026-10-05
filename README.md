# Jev Agent-First Install Kit

[![CI](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml)

This project lets a coding agent set up Jev routing for **Codex**. Jev can choose
a model and reasoning depth for a task when you select `jev/auto`; you can still
select a model yourself. The repository includes an installer, an embedded
router, and checks that report what actually worked on your computer.

**Current status: public alpha.** The installer supports a *fresh Codex profile*
on macOS, Linux, and Windows. It cannot migrate an existing profile or replace
an existing router. An agent may run inside Codex, Claude Code, Gemini CLI, or
another coding environment, but the installed integration currently targets
Codex only.

## Before you start

You need:

- Codex CLI, Python 3.11+, Node.js 22.19+, npm, and a supported per-user service
  manager (`launchd`, `systemd --user`, or Windows Task Scheduler).
- A TypeSafe or OpenRouter API key for Jev's routing decisions. The installer
  asks for it in a hidden terminal prompt and stores it outside this repository.
- A fresh Codex profile and free service ports. If your intended profile or
  router is already in use, the agent should stop and explain the conflict.
- A Codex/ChatGPT sign-in if you plan to use native GPT models. You complete
  sign-in and approve session sharing yourself after installation.

**OmniRoute is optional.** If you already have a working OmniRoute gateway, the
installer can connect to it using a protected auth file. During installation
you choose your own provider/account order and model mappings, or supply an
existing protected configuration. See [provider configuration](docs/PROVIDERS.md). If
you do not have one, choose **no** and use native Codex routing. This installer
does not create an OmniRoute gateway or provider accounts.

## Give it to an agent

Send the agent the repository link and this request:

> Install `https://github.com/teo-nex/jew-codex-harness` for a fresh Codex
> profile. Read `AGENT_INSTALL.md` and `AGENTS.md` first. Check my intended
> profile, service ports, prerequisites, and disk space before changing anything.
> Stop and tell me if migration of an existing profile or router is needed;
> do not silently pick another profile. Run the interactive `onboard` installer
> for a fresh profile. Ask me whether I already have OmniRoute and let me choose
> my provider order and exact model IDs. Let me enter
> the TypeSafe or OpenRouter key only in the local hidden prompt, never in chat.
> Afterwards, tell me what passed, what remains untested, and which steps I
> must complete in Codex before calling the installation working.

The agent performs preflight, preparation, a dry run, installation, and local
verification. It cannot approve Codex hooks, sign in to your account, or grant
native session sharing on your behalf. Those steps require your review.

## Run it yourself

On macOS or Linux:

```sh
git clone https://github.com/teo-nex/jew-codex-harness.git
cd jew-codex-harness
./install.sh onboard
```

On Windows, in PowerShell:

```powershell
git clone https://github.com/teo-nex/jew-codex-harness.git
Set-Location jew-codex-harness
.\install.ps1 onboard
```

The wizard asks for a fresh profile path, whether your Jev key is from TypeSafe
or OpenRouter, and whether to use an existing OmniRoute gateway. For OmniRoute,
it can build your protected provider sequence interactively. It checks for
structural blockers before asking for the key. Keep the checkout at a stable
path: installed services and hooks refer to it. See the [installation guide](docs/INSTALL.md)
for non-interactive commands, protected files, and rollback.

## Finish in Codex

1. Open a fresh Codex session in the installed profile. Use `/hooks` to review
   and trust the new hooks.
2. Sign in if needed. For native GPT routing, explicitly enable session sharing
   for this profile as described in the [installation guide](docs/INSTALL.md).
3. Select `jev/auto` when you want automatic routing. Your previous default
   model is preserved, so the installer does not switch you to `jev/auto`.
4. From the repository root, run
   `python3 -m harness.cli --codex-home /path/to/new-profile verify --live --manual-model MODEL_ID`
   with a model available to that profile (`py -3` instead of `python3` on
   Windows). The [installation guide](docs/INSTALL.md) explains what each
   check proves.

A green installer result means the local files and services passed their checks.
Call the setup **verified** only after a real response completes through the
intended Codex profile. Browser, Computer, and context-compaction behavior need
separate checks in a client that exposes those capabilities.

## What is included

- A Codex adapter and installer for fresh profiles on macOS, Linux, and Windows.
- A local Jev router with native Codex routing and an optional user-configured
  [OmniRoute provider order](docs/PROVIDERS.md). [Reasoning profiles](docs/REASONING.md) can map Jev's chosen
  effort to supported levels on each configured destination model.
- An optional CanvasTTY browser broker on macOS when its helper is already
  installed. CanvasTTY and visible terminal workers are not installed by this kit.
- A 24-hour API-price comparison after real routed work. It estimates what the
  observed tokens would cost at listed API prices; it does not measure money
  paid, subscription savings, or equal task quality.

The [support matrix](docs/SUPPORT.md) shows what has and has not been tested on
each operating system. [GitHub Actions](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml)
runs offline installation checks with synthetic credentials on all three. CI
does not prove a first installation with your credentials, hook trust, OAuth,
or a completed live model response.

## Managing an installed profile

Run commands from the checkout with `python -m harness.cli --codex-home PROFILE`.
Use `py -3` instead of `python` on Windows.

| Control | Command or configuration |
| --- | --- |
| Provider/account order and models | `routes show`, `routes apply --config FILE`, `routes reorder ID ...` |
| Safe edits and recovery | Config validation, durable backups, crash recovery, `routes rollback` |
| Advertised model IDs | `catalog`, `routes check --catalog`; warnings for missing IDs and cross-family aliases |
| Request diagnostics | `explain`: route, requested/effective effort, attempts and timings without prompt or credential export |
| Project restrictions | Native-only, allowed providers, no paid fallback, required final Astra |
| Failure handling | Connection/first-token/idle/total/attempt budgets and isolated account/model cooldowns |
| Acceptance | `acceptance --offline-recovery`; live response/tool/effort checks require explicit `--live` |

Project policies, request budgets and failure cooldowns are described in
[Providers](docs/PROVIDERS.md). Opt-in fresh-client acceptance is documented in
[Installation](docs/INSTALL.md); offline versus live evidence is recorded in
[Improvements](docs/IMPROVEMENTS.md).

## More information

- [Agent installation protocol](AGENT_INSTALL.md) and [adapter contract](docs/ADAPTER-CONTRACT.md)
- [Installation details](docs/INSTALL.md), [CI scope](docs/CI.md), and [support matrix](docs/SUPPORT.md)
- [Reasoning effort routing](docs/REASONING.md) and [source provenance](ROUTER_FORK.md)
- [Provider/model routing audit](docs/AUDIT-2026-10-05.md)
- [Security policy](SECURITY.md) and [contributing guide](CONTRIBUTING.md)

The root project is [MIT licensed](LICENSE). The embedded router retains its
own [MIT license](router/LICENSE) and [attribution](router/NOTICE.md). Keep
API keys, OAuth files, provider state, and runtime logs out of Git.
