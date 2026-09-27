# Target-client adapter contract

An installation agent can run in any harness. A **target-client adapter** is
needed for each client receiving Jev behavior. Codex is the bundled adapter;
this document defines the minimum for adding another one.

## Required mapping

Before writing files, identify the target client's supported extension points
and native permission model. Record one of `SUPPORTED`, `UNSUPPORTED`, or
`UNKNOWN` for each capability:

| Capability | Adapter must connect |
| --- | --- |
| Model routing | A model request boundary where Jev can choose among a closed, available model set before execution. Keep one conversation across provider fallback only where the client's history format supports it. |
| Tool Use | A pre-action hook with the real current user goal and proposed side effect. Safe reads may skip Jev; writes and external actions need host permission plus Jev review in auto mode. Manual model selection must not acquire an ordinary Jev veto. |
| Browser/Computer Use | A live observation and element-specific action boundary, host-owned execution, and post-action verification. Never treat a Jev choice as permission. If the client exposes no such boundary, mark it unsupported. |
| Context compact | A before/after compact boundary. Jev may shortlist carry facts; the client retains responsibility for transcript compaction. |
| Skills/workers | A bounded skill index and native worker lifecycle with file ownership, checks and collected results. Do not spawn hidden children or pass whole transcripts by default. |
| Usage/cost | Provider usage and missing-usage coverage. Separate observed usage, synthetic tests, API-price counterfactuals and account debits. |

## Safety and ownership

- The client owns capability discovery, permissions, approvals, execution,
  retries, cancellation, and final answers. Jev receives bounded choices and
  returns advice, never executable shell text or a grant of permission.
- Routing may fall back to a known safe native model before any client-visible
  output. A partially delivered response must not be replayed to a second
  provider without a client-supported recovery contract. Risky tool actions
  fail closed when authorization cannot be established.
- Keep credentials in protected local files or a local hidden prompt. Never
  write values into repository files, tool arguments, agent prompts, reports or
  replay cases. Preserve unrelated client configuration and active sessions.
- Keep a journal of files and service identities owned by the adapter. A
  rollback may remove only exact owned, unchanged resources. An existing
  profile requires an explicit migration plan, not an overwrite.

## Acceptance before claiming support

Use synthetic tasks first, then a real client session. Verify a safe read,
scoped edit with independent tests, denied unrelated destructive action,
manual-model autonomy, one completed routed response, provider failure before
output, and all UI/compact capabilities claimed by the adapter. Check the
target's actual process and config, not only files or localhost health.

Add the adapter's tests and a support-matrix row before describing it as
bundled. Until then the agent-first installer reports `UNSUPPORTED_TARGET` or
`PARTIAL` for that client.
