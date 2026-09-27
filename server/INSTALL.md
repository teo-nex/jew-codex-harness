# Jev server operations

Use the repository's [agent contract](../AGENTS.md) and [installation guide](../docs/INSTALL.md). The supported installer is `python3 -m harness.cli` (`py -3` on Windows). It provisions a fresh Codex profile, the embedded router, a per-user Jev service, hooks and MCP workers. `server/install-service.sh` and `server/watchdog.sh` are historical upstream utilities; they do not install the full harness or provide its ownership journal.

The Jev service binds `127.0.0.1` at the configured port (4319 by default). The router uses 4202 by default. `verify` checks local files/configuration and service health; `verify --live` checks one typed Jev decision, one synthetic Codex `jev/auto` turn, the prompt-hook session marker, and priceable usage telemetry. Neither health alone nor a router catalog entry proves an actual completed user task. For Browser/Computer/Compact, check the capability in the real Codex client after installation.

Protected state lives under the selected `CODEX_HOME`; the Jev decision key (TypeSafe or OpenRouter) and optional OmniRoute key are referenced by protected file paths and must not be copied into this repository. The selected model receives the complete canonical request. Jev sees a bounded decision state. The service never binds a public interface. Review and redact any local log before sharing it.

## Recovery

- The kill-switch file `CODEX_HOME/codex-router/jev-router.off` bypasses Jev decisions until removed. It does not repair provider credentials or a broken transport.
- After an OAuth checkpoint, inspect `CODEX_HOME/jev-harness/journal.json` and use `resume` as described in the installation guide. A partial router bootstrap or foreign file requires manual review.
- `rollback` checks service identity and managed file hashes. It retains the profile and router state for inspection; it does not remove arbitrary files or rewrite an old full `config.toml` over later edits.
- Source updates are reviewed Git changes. `bin/jev-codex-router update` is disabled in this standalone project. Test the new checkout and arrange a quiet service restart for an existing deployment.
- For a stream failure, use the local decision log's stage/status/terminal and bounded event-type diagnostics. Never upload raw prompts, tool arguments or credentials. A tool call already exposed to Codex must not be replayed automatically.

Current platform coverage and known gaps are in [SUPPORT.md](../docs/SUPPORT.md).
