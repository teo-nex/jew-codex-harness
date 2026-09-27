# Support and verification matrix

Last reviewed: 2026-09-26. This repository is a private alpha for **fresh Codex profiles**. Existing installations require a separate, reviewed migration.

Any coding agent may follow the agent-first entry point. This does not imply
that its own harness is an installed target: Codex is the only bundled target
adapter. See [AGENT_INSTALL.md](../AGENT_INSTALL.md) and
[the adapter contract](ADAPTER-CONTRACT.md). The agent reports each capability
as verified, blocked, unsupported or untested on the target host.

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Jev server; native or optional OmniRoute ladder | Unit tests and local canary passed for existing routes; fresh onboarding pending | Offline tests; real host pending | Offline/mock tests; real host pending |
| Background service | launchd adapter, mocked tests | systemd user adapter, mocked tests | Task Scheduler adapter, mocked tests |
| Global Codex hook/context | Packaged; fresh profile loading pending | Packaged; real loading pending | Packaged; ACL and real loading pending |
| `jev-workers` | CanvasTTY path | Read-only Codex CLI tasks only | Read-only Codex CLI tasks only |
| CanvasTTY browser broker | Optional when helper exists | Unavailable | Unavailable |
| Fresh profile end-to-end install | Pending on a fresh profile | Pending | Pending |

Python 3.11+, Node.js 22.19+, npm and Codex CLI are required. Provider OAuth and protected key files are supplied by the account owner. No CI job uses live provider credentials. See [CI](CI.md) for the offline matrix and [installation](INSTALL.md) for accepted inputs and rollback limits.

A successful source test does not prove client loading or model quality. Report separately: files written, service health, Codex loading, and a real completed `jev/auto` response. Existing-profile migration and mutable Linux/Windows workers remain outside this alpha's automatic install path.
