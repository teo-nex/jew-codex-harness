# Jev Agent-First Install Kit

Give this repository to **any coding agent** as the installer. The agent's own
harness can be Codex, Claude Code, Gemini CLI, Hermes, or another environment:
it follows [AGENT_INSTALL.md](AGENT_INSTALL.md), runs preflight, installs only a
supported target adapter, and reports what actually worked. The target adapter
included today is **Codex**. Other target clients need an adapter following
[the adapter contract](docs/ADAPTER-CONTRACT.md); agent instructions alone
cannot make Codex hooks run in another client.

The included Codex installer targets a **fresh** profile on macOS, Linux or
Windows. It contains a pinned Codex Router snapshot plus Jev routing, guard,
context helper and worker integration. The native-only route needs no
OmniRoute; an optional provider ladder uses an existing OmniRoute gateway when
explicitly configured. Accounts, OAuth sessions, keys and runtime state stay
outside Git.

Copy this task to your agent:

> Read `AGENT_INSTALL.md` and `AGENTS.md` in this repository. Install the
> supported Jev integration for my target client, preserving my current model,
> profiles and running sessions. Ask for credentials only through a local hidden
> prompt or protected file. Verify files, service, client loading and one real
> model response separately. Report unsupported capabilities and blockers; do
> not call installation complete based only on tests or a healthy service.

Короткое задание на русском: **«Прочитай `AGENT_INSTALL.md` в этом репозитории.
Определи мой целевой клиент, установи только поддержанный адаптер и проверь
его в новом сеансе. Сохрани текущую модель и работающие службы. Ключи получай
локально, в чат не выводи. Верни статус каждой функции и точные блокеры».**

For Codex, the agent runs the existing installer. A **fresh profile is required**
for automatic installation; an occupied router or existing profile produces a
reviewable migration report rather than an overwrite. This repository is a
private alpha, and a target host must pass its own live acceptance before the
agent calls it working. No VM is needed merely to use the agent-led installer
on the target machine.

For an interactive fresh-profile setup, run `./install.sh onboard` (Windows:
`./install.ps1 onboard` in PowerShell). It asks for a TypeSafe or OpenRouter Jev
key without echoing it, then asks whether to use an existing OmniRoute gateway.
It checks the host before collecting a new key. Existing profiles and occupied
router services require a reviewed migration; the wizard does not replace them.
After installation, review the new hooks with `/hooks` in a fresh Codex session.
Follow [the installation guide](docs/INSTALL.md) for protected-file and
non-interactive commands.

After real work, use the wizard's `report_command` to show a reproducible
24-hour API-price comparison with GPT-6 Sol and Astra. It reports data coverage
and never treats subscription credits or a hypothetical baseline as money paid.

To prepare a reviewable local source archive, follow [the release checklist](docs/RELEASING.md). The packager uses a clean committed tree and verifies the archive before reporting success.

The installer preserves the current default model. Selecting `jev/auto` enables the provider ladder. Existing live profiles require a separately verified migration; this installer will stop without changing them. See [source provenance](ROUTER_FORK.md).

Optional [reasoning profiles](docs/REASONING.md) map Jev's selected effort to
each destination model during automatic provider fallback. Unknown models keep
legacy behavior; per-attempt diagnostics distinguish requested and mapped depth.
