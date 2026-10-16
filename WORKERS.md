# Worker permissions

The default `jev-workers` backend uses the Codex CLI on macOS, Linux and
Windows. CanvasTTY is not required for coding tasks.
Inside a macOS CanvasTTY agent session, the existing native terminal backend
remains the default: its outer isolation cannot launch a nested Seatbelt
sandbox (`sandbox-exec: sandbox_apply: Operation not permitted`). No sandbox
is disabled to work around that restriction. `JEV_WORKER_BACKEND` explicitly
selects `portable` or `canvastty`; the latter requires an installed macOS helper.
Linux requires a host that permits Codex's Bubblewrap/user namespaces. The
ephemeral Ubuntu CI runner explicitly enables that prerequisite; the installer
does not change host kernel or AppArmor policy. A sandbox startup failure is
not a reason to disable the worker sandbox.

Pass `request_id`, `goal`, an absolute `cwd`, and explicit relative
`allowed_files`. An existing directory grants ownership of its descendants;
a missing path grants only that exact path. Overlapping ownership, traversal,
symlinks and hard-linked write targets are rejected. The worker cannot own
the harness source, its Codex profile or worker state.

The runner requires this checkout's unfiltered `PreToolUse` hook in the target
profile. Install/verify the profile and review hooks in Codex before use; the
runner does not change hook trust or modify the profile.

Coding workers run with `workspace-write`, hooks enabled, network disabled,
MCP/plugins disabled and nested agents disabled. A private manifest and its
SHA-256 bind every tool call to the submitted task. This guard also applies
when a routed provider reports a concrete model ID instead of `jev/auto`.

Writes use `apply_patch`: every source and move destination must be owned,
then Jev must authorize the edit. Shell calls allow a small set of reads
(`cat`, `rg`, `ls`, `head`, `tail`, and corresponding PowerShell reads).
Shell writes, interpreters, arbitrary MCP calls and nested agents are denied.
The portable backend currently rejects UI/document capabilities; the macOS
CanvasTTY backend remains optional via `JEV_WORKER_BACKEND=canvastty` in the
MCP server environment. It must already be installed.

For analysis, set `read_only: true` and supply empty `allowed_files` and
`acceptance_commands`. This retains the Codex read-only sandbox.

The harness runs submitted `acceptance_commands` after the model exits and
after Jev authorizes those commands. Commands are argument arrays, never shell
strings. They are trusted task checks running with the harness user's rights;
they are not confined to `allowed_files`. Do not use untrusted check commands.
Results include changed paths, a reviewable patch, check exit codes and private
log paths. A coding turn without a guard receipt, a denied tool, a Jev denial
or a failing check/tool cannot report `completed`. A coding task with no actual
file changes is reported as `worker_no_changes` for the owner to review.

These are application guards plus the Codex workspace sandbox, not a VM or
an OS sandbox with a per-file allowlist. They assume a trusted Codex executable,
hook sources, shell environment and task checks. Source tests and synthetic
client tests do not establish live provider acceptance on another host.

Run `python tests/worker_client_acceptance.py` for a fresh real CLI against a
local synthetic provider and synthetic Jev decisions. It checks an owned edit,
an ownership escape and a Jev denial. It uses an isolated profile and bypasses
hook trust only for that fixture's generated hook file. The CI matrix runs
this check on all three OSes; a workflow change is not a passed hosted run.
