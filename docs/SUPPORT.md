# Support and verification matrix

Last reviewed: 2026-10-16. This is a public alpha. Automatic installation supports **fresh Codex profiles** only. Existing installations require a separate, reviewed migration.

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
| `jev-workers` | Guarded coding CLI; optional CanvasTTY | Guarded coding CLI | Guarded coding CLI |
| CanvasTTY browser broker | Optional when helper exists | Unavailable | Unavailable |
| Hosted fresh-profile install smoke (synthetic key; no live provider) | Passed in CI | Passed in CI | Passed in CI |
| Live first install with owner credentials and completed model response | Pending | Pending | Pending |

Python 3.11+, Node.js 22.19+, npm and Codex CLI are required. Provider OAuth and protected key files are supplied by the account owner. No CI job uses live provider credentials. See [CI](CI.md) for the offline matrix and [installation](INSTALL.md) for accepted inputs and rollback limits.

A successful source test does not prove fresh-client hook trust or model quality.
Report files, service health, Codex loading and a real completed `jev/auto`
response separately. Existing-profile migration remains outside the automatic
install path. CLI coding workers use explicit ownership and Jev review on all
three OSes; see [worker permissions](../WORKERS.md) for the enforced boundaries
and the distinction between task checks and worker tools. CanvasTTY and its
macOS browser broker are optional; portable coding does not require them.

The hosted worker-client matrix uses the actual Codex CLI with an isolated
profile, synthetic loopback model responses and synthetic Jev decisions. It
requires a real owned UTF-8 edit and patch artifact, plus rejection of both an
out-of-ownership patch and a Jev-denied patch. Consult the
[workflow results](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml)
for the tested revision and OS results; configuring a job is not a passed run.
The fixture alone bypasses trust for its generated hook file; production
workers require reviewed hooks. A macOS CanvasTTY session cannot start a
nested Seatbelt sandbox, so it retains the native CanvasTTY worker backend.
No live provider or installed-profile acceptance is claimed by source or
synthetic client checks.

## CanvasTTY native orchestration with jev/auto

CanvasTTY Orchestrator sessions use their own `canvastty_agents` MCP server.
In sessions with complete inherited CanvasTTY runtime metadata, the global hook
returns no permission verdict for any tool. CanvasTTY validates caller identity,
child ownership, project directory, profile ceiling, isolation and launch options;
its own hooks continue to enforce tool permissions in Auto and Bypass. Arguments
pass through unchanged. Jev selects only models and reasoning depth. Outside
CanvasTTY, existing Jev guards remain unchanged; an MCP tool name alone does not
select the CanvasTTY permission policy.

Select `jev/auto` for each child that should route through Jev and start CanvasTTY
with the intended `CODEX_HOME`. An external route must also support the client's
tool search history as described in [provider configuration](PROVIDERS.md).

Native orchestration does not add the `jev-workers` manifest, file allowlist or
acceptance and transfer workflow. Outside CanvasTTY, use `jev-workers` when
that workflow is needed.
