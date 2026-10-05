# Routing improvements acceptance ledger

Each numbered feature is committed independently. Offline assertions are not
live provider or reasoning-enforcement proof. Existing services are untouched.

| Feature | Observable acceptance | Status |
| --- | --- | --- |
| 1 | Owned route edits, reorder, durable backup, crash recovery and rollback without a service restart; foreign edits refused | verified: `tests/test_core.py` hot-route tests |
| 2 | Catalog picks exact advertised IDs; missing models and cross-family aliases warned | verified: `tests/test_catalog.py` and onboarding regression tests |
| 3 | Latest request explained with sanitized routing, effort and timing evidence | verified: `tests/test_explain.py`, server response/signature regressions |
| 4 | Project rules restrict transport/provider/payment and enforce final Astra | verified: `server/test_project_policy.py`; live project binding not run |
| 5 | Connect, first token, idle, total and attempt limits stop hung requests safely | verified: loopback slow-stream tests; all 250 server tests (1 platform skip) |
| 6 | Bad payload does not poison account; auth/quota/transient cooldowns isolate accounts | verified: all 255 server tests (1 platform skip), including account/model recovery |
| 7 | Opt-in fresh-client response/tool/effort checks and controlled recovery report evidence separately | verified: real fresh Codex client with synthetic `[503, 200]`, two requests, exact marker and matching thread scope; live paid/provider effort checks NOT RUN |

## Final local verification

Checked on 2026-10-06. These results apply to the local source tree, not a
deployment into the user's running profile.

- Harness: `python3 -m unittest discover -s tests -p 'test_*.py' -q`:
  174 tests, 173 passed, one platform skip.
- Server: `python3 -m unittest discover -s server -p 'test_*.py' -q`:
  257 tests, 256 passed, one platform skip.
- Router: `npm run check` passed; browser broker tests passed (7 tests).
- Full Node suite: 4447 tests, 4408 passed, 7 failed, 32 skipped. All seven
  failures occur in interactive provider-key/setup tests at `os.forkpty()`:
  macOS returns `OSError: [Errno 6] Device not configured` before CLI execution.
  Running those two test files sequentially and a standalone `os.forkpty()`
  control reproduced the same error. These checks remain environment-blocked;
  the full Node suite is not claimed as passed. No router sources were changed.
- Offline configuration smoke passed all six isolated steps.
- Controlled recovery passed in a disposable profile with a real new Codex
  process, two synthetic loopback requests, `[503, 200]`, an exact accepted
  marker and independently correlated thread scope. Live provider identity,
  actual billing and internal reasoning enforcement remain unverified.

Existing profiles, credentials, services and user terminals were not modified.
Live acceptance is explicit opt-in; no live provider generation was run.
