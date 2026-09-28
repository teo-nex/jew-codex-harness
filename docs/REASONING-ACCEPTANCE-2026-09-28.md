# Reasoning Acceptance: 2026-09-28

## Offline Acceptance

- Server suite: 224 tests, 223 passed, 1 skipped.
- Harness suite: 154 tests, 153 passed, 1 skipped.
- Total: 376 passed, 2 skipped.
- New coverage: exact model profiles, schema rejection, monotonic mapping,
  explicit unsupported control, retry isolation, original effort preservation,
  requested/effective/source telemetry, unchanged native payload behavior,
  strict smoke output validation and bounded stream deadlines.
- Python compilation and whitespace checks passed.

Commands: `python3 -m unittest discover -s server -p 'test_*.py' -q`
and the same command with `-s tests`. The existing embedded Node router was
not modified; its full suite was not rerun for this Python-only feature.

## Live Gateway Acceptance

All calls used synthetic input through the existing loopback OmniRoute gateway.
The smoke reused the production effort resolver and Responses payload shaper.
No services, client profiles, provider accounts or gateway settings changed.

| Requested route | Low JSON | High JSON | Two-request tool loop |
| --- | --- | --- | --- |
| antigravity/gemini-3.8-flash-tiered | PASS | PASS | PASS |
| wally/glm-5.3-flash | PASS | PASS | PASS |
| gonkagate/deepseek-ai/deepseek-v4-flash-0731 | FAIL | FAIL | FAIL |

Wally additionally passed a real `max -> high` mapped-effort request.
Successful cases required a completed response and the exact JSON integer
result, not just HTTP 200. The tool loop required the expected function name,
arguments and call ID, followed by a correct final response after the result.

DeepSeek failed strict JSON parsing. A targeted repeat confirmed a completed
response containing repeated JSON objects and extra prose. Another simpler
diagnostic prompt returned a single JSON object; this does not waive the failed
matrix. DeepSeek is NOT accepted as a reliable route by this test. Its existing
ladder position was not changed by this feature.

## Limits

These results establish gateway acceptance and basic model/tool behavior, not
provider-internal enforcement of reasoning depth, model quality improvement,
latency improvement, or a fresh installed Codex-client E2E test. Every live
result explicitly labels provider control as NOT_VERIFIED. Candidate profiles
are operator contracts, not a universal capability catalogue.

Gemini workers ran in owned CanvasTTY terminals. Initial worker-generated
test code and PASS claims were rejected when independent execution failed;
the counts above come from the corrected, independently executed suites.
No credential values or raw reasoning text are included in this report.
