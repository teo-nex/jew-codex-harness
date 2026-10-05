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

## Published-source verification

The changes were published to `main` on 2026-10-06 with each feature kept in
its own commit. GitHub API publication produced new commit IDs; every published
Git tree was checked against its local original before advancing `main`.

The [first hosted run](https://github.com/teo-nex/jew-codex-harness/actions/runs/37383676900)
exposed two test-fixture problems: an unmocked second Windows ACL check, and
competing idle/total deadlines in the network timing test. The fixture-only
correction preserves production ACL enforcement and request limits. Added
negative credential coverage and a deterministic deadline/stale-timer check.

After that correction, local harness tests ran 175 tests (174 passed, one
platform skip) and server tests ran 258 tests (257 passed, one platform skip).
The 32 focused routing/transport and seven browser broker tests also passed.
A new real Codex process again accepted the synthetic `503 -> 200` recovery
with two requests and a matching fresh scope; live generation remained off.

The hosted workflow now also runs the full Node regression suite and the
fresh-client recovery on Linux, macOS and Windows. Consult the
[published-commit Actions run](https://github.com/teo-nex/jew-codex-harness/actions/workflows/ci.yml)
for its status; configured checks or a successful local run are not a claim
that hosted checks passed. Real-provider identity and reasoning enforcement
remain outside these synthetic checks.

## Bounded-client correction

The [expanded run](https://github.com/teo-nex/jew-codex-harness/actions/runs/37384636775)
passed all six Python jobs, all three install/rollback smoke jobs and the full
router/fresh-client jobs on Linux and macOS. The Windows fresh-client step
outlived its subprocess timeout; its exact cause was not established from
the still-running job. It must not be recorded as a passed client check.

Verification runners now close stdin, capture output in files instead of
inherited pipes, and terminate only their owned process tree on timeout.
The synthetic client preserves Windows system variables case-insensitively
while assigning all profile/app-data paths to its disposable fixture. CI adds
a separate three-minute ceiling for this step. No live profile is changed.

Local regression after this correction: 180 harness tests (179 passed, one
platform skip), 259 server tests (258 passed, one platform skip), and a real
fresh Codex recovery with exactly two synthetic requests, `[503, 200]`, the
expected marker and a matching thread scope. Process tests cover EOF on stdin,
descendant cleanup after timeout, inherited output handles, nonzero exit codes
and bounded capture. Hosted Windows acceptance still requires the new run.

## Deadline attribution and repeated Windows checks

An early socket timeout could be reported as `idle` even when the socket's
wait was limited by the earlier total deadline. Failure classification now
uses the binding deadline when there is no explicit timeout phase. A
deterministic test reproduces a socket timeout just before monotonic reaches
the total deadline; all 260 server tests passed locally (one platform skip).

Fresh-client failure reports expose only a fixed stage, exception class and
numeric OS error, never raw client stderr, prompts or credential material.
Cleanup diagnostics add only fixed fixture-area and file-kind labels.
Windows CI runs three independent recovery processes with new disposable
profiles; failures are not retried or hidden. Its three-minute step ceiling
remains in addition to each client's bounded timeout.

The repeated Windows run exposed `PermissionError` during temporary-directory
cleanup after both provider requests, not a timeout or missing route. Owned
fixture servers now stop and wait for their request handlers before restoring
patched globals or removing files. Tests enforce this teardown order and check
that partial server startup closes sockets without waiting for an unstarted
server. The follow-up hosted run must still pass all repeated Windows checks.

## Process-owner startup race

The [expanded hosted run](https://github.com/teo-nex/jew-codex-harness/actions/runs/37387416583)
passed all Python, install-smoke and repeated fresh-client checks, and the full
Linux router suite. Its macOS suite exposed a process-owner startup race:
an intermediate owner could be descheduled immediately after spawning its
child, before its native signal handlers were installed. An early signal then
terminated that owner without forwarding cleanup to its detached descendant.

A fixture now deliberately delays that registration by 600 ms. It reproduced
the missing rollback marker against the old code and passed after handlers
were moved before spawn. Both the delayed and ordinary regression preserve
the original rollback and bounded-shutdown assertions. Cross-platform full
suite results still require the follow-up published run.

## App-server test containment

The Windows router suite also found a locked disposable app-server profile.
The test waited for its launcher to exit but did not retire descendants. It
now uses the existing kill-on-close Windows Job Object (or its own POSIX
process group), waits for tree cleanup and stream closure, then removes only
its fixture directory. A synthetic descendant regression failed against the
old helper and passed after the fix. All four local app-server assertions,
including both real signed-out Codex provider variants, passed.
