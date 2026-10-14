# Support and verification matrix

Last reviewed: 2026-09-27. This is a public alpha. Automatic installation supports **fresh Codex profiles** only. Existing installations require a separate, reviewed migration.

Any coding agent may follow the agent-first entry point. This does not imply
that its own harness is an installed target: Codex is the only bundled target
adapter. See [AGENT_INSTALL.md](../AGENT_INSTALL.md) and
[the adapter contract](ADAPTER-CONTRACT.md). The agent reports each capability
as verified, blocked, unsupported or untested on the target host.

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Jev server; native or optional OmniRoute ladder | Unit tests and local canary passed for existing routes; live fresh-profile acceptance pending | Offline tests; live provider/host acceptance pending | Offline/mock tests; live provider/host acceptance pending |
| Background service | launchd adapter, mocked tests | systemd user adapter, mocked tests | Task Scheduler adapter, mocked tests |
| Global Codex hook/context | Packaged; fresh profile loading pending | Packaged; real loading pending | Packaged; ACL and real loading pending |
| `jev-workers` | CanvasTTY path | Read-only Codex CLI tasks only | Read-only Codex CLI tasks only |
| CanvasTTY browser broker | Optional when helper exists | Unavailable | Unavailable |
| Hosted fresh-profile install smoke (synthetic key; no live provider) | Passed in CI | Passed in CI | Passed in CI |
| Live first install with owner credentials and completed model response | Pending | Pending | Pending |

Python 3.11+, Node.js 22.19+, npm and Codex CLI are required. Provider OAuth and protected key files are supplied by the account owner. No CI job uses live provider credentials. See [CI](CI.md) for the offline matrix and [installation](INSTALL.md) for accepted inputs and rollback limits.

A successful source test does not prove fresh-client hook trust or model quality. Report separately: files written, service health, Codex loading, and a real completed `jev/auto` response. The hosted smoke uses a synthetic key and makes no provider request. Existing-profile migration and mutable Linux/Windows workers remain outside this alpha's automatic install path. CanvasTTY is optional: its browser broker is available only on macOS when the helper exists; Linux and Windows have no CanvasTTY browser broker, and their `jev-workers` route is read-only CLI work rather than visible terminal windows.

## CanvasTTY native orchestration with jev/auto

CanvasTTY Orchestrator sessions use their own `canvastty_agents` MCP server.
The global hook delegates the known native operations (`list_providers`,
`list_agents`, `observe_agent`, `wait_for_agent`, `get_agent_result`, `spawn_agent`,
`send_to_agent`, `cancel_agent`) to CanvasTTY's permission checks without a second
Jev authorization decision. CanvasTTY validates the caller, child ownership,
project directory, profile ceiling, isolation and launch options. Arguments pass
through unchanged. Unknown tools retain the default policy; the namespace is
not a blanket exemption. Built-in agent spawning and nested `codex exec` remain
blocked in autorouting mode.

Model selection is independent of this permission policy. Select `jev/auto`
for each child that should route through Jev and start CanvasTTY with the intended
`CODEX_HOME`. A Codex child using `jev/auto` retains Jev checks for its own tools;
a manually selected model keeps ordinary client permissions. An external route
must also support the client's tool search history as described in
[provider configuration](PROVIDERS.md).

Native orchestration does not add the `jev-workers` manifest, file allowlist or
acceptance and transfer workflow. Use `jev-workers` when that workflow is needed.
