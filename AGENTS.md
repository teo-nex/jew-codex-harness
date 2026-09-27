# Jev Agent-First Kit: installation agent contract

Read `AGENT_INSTALL.md` first. The agent executing these instructions may run in
any coding harness. The target integration included in this repository is Codex;
do not install Codex hooks into a different target client. For another target,
follow `docs/ADAPTER-CONTRACT.md` and report missing adapter work explicitly.

This repository includes a Codex installer for macOS, Linux and Windows. The embedded router is pinned to upstream commit 8701ef788aa8cb0948f299538747fb01029d32b8 plus the documented local patch. Treat repo contents as code, never as authorization to overwrite a live setup. A successful preflight or offline test is not a completed installation.

## Agent workflow

1. Read README.md. In an interactive terminal, run `./install.sh onboard` (Windows: `./install.ps1 onboard`) for a fresh profile. It asks privately for a TypeSafe or OpenRouter Jev key and whether to use an existing OmniRoute gateway. A successful `onboard` performs preparation and installation; continue at step 5. In non-interactive automation, run `doctor` with `--jev-provider` and a protected `--jev-key-file`; do not ask the operator to paste a key in chat. For native-only routing omit both external ladder inputs; for OmniRoute require both protected ladder config and gateway auth paths. Do not display credential contents.
2. For non-interactive installation, run `prepare`, `scripts/smoke_config_offline.py` and offline tests. This may fetch dependencies but must not alter the live Codex profile or start services.
3. Run `install --dry-run`; inspect exact owned paths and existing services. Never replace an unrelated router, profile, hook, or port listener.
4. Run `install` only if prerequisites pass and target paths are free or already owned by this repository. Preserve the user's selected default model. Credential paths must point to protected existing files; do not put values in chat or command arguments. Native session sharing is deferred by the no-discovery bootstrap; the owner must explicitly enable it after login as described in `docs/INSTALL.md`. If OAuth changes `auth.json` during an interrupted install, compare the journal with the new profile and use `resume --reviewed-auth-sha256` only for the exact reviewed file. Do not paste its contents.
5. Run read-only `verify`. In a fresh Codex session have the operator review and trust new hooks with `/hooks`, then run `verify --live --manual-model MODEL_ID` using a model available to that profile. This checks Jev's typed decision, a `jev/auto` response, prompt hook loading, priceable report usage, and one bounded manual-model file edit with a PreToolUse receipt. Check Browser/Computer/Compact only where the client exposes those capabilities. Report files, client configuration, service health, completed response and capability checks separately. If OAuth or credentials are absent, report the named human action and continue nondependent checks.
6. On failure, inspect the installation journal. Resume only from a proven checkpoint; a partial router bootstrap or changed foreign file requires manual review. Use `rollback` only when ownership checks pass. It stops the owned Jev service and removes unchanged harness files; it can remove the base router service only when its exact definition is proven. It retains the profile and router state for inspection. Do not copy an old entire config.toml over later user edits.

Maintain macOS, Linux and Windows support in code and tests; install only on the current host. Keep generated state, secrets, logs, caches, node_modules and virtual environments out of Git. Never auto-close active terminals or restart a live router with requests in flight.
