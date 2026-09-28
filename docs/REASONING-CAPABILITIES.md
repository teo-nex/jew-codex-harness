# Reasoning boundary evidence

The existing Jev classifier already chooses `low`, `medium`, `high`, `xhigh`
or `max` independently of model capability (`server/routing_policy.py`).
Before this feature, `_serve_ladder` in `server/jev_server.py` forwards that
same value to every destination through Responses `reasoning.effort`.
That is a requested control, not evidence that a provider implements it.

## Gateway boundary

Read-only inspection of the running local OmniRoute build on 2026-09-28
found `applyThinkingBudget` in the installed build chunk
`open-sse_0uqtwu6._.js`. Its default is `passthrough`. Its `auto` mode removes
`thinking`, `reasoning_effort`, `reasoning`, and generation thinking config.
The runtime setting was not changed or assumed from the default.

Consequently, a completed response only proves gateway acceptance and model
completion. It does not prove upstream enforcement or comparable reasoning
quality. A live test must keep those claims separate. Do not change the
shared gateway to make a smoke test pass.

## Capability contracts

Use exact destination IDs in explicit `reasoning_profiles`. Do not infer a
provider's controls from a substring such as `gemini` or `deepseek`, or from
the model's presence in `/v1/models`. Unknown profiles preserve compatibility
and must remain labeled unknown. Configured profiles express the operator's
contract; they are not a claim of upstream verification.

Verify schema rejection, original-effort reuse after fallback, absent effort,
unsupported controls, and preservation of canonical input with offline tests.
For live tests use synthetic prompts and completed-response validation through
the existing gateway, recording requested/effective controls without secrets
or reasoning text. Record unavailable endpoints as blocked, not passed.
