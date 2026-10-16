# Compact after 30 minutes idle

The optional managed launcher asks the native Codex app-server to compact an
idle thread after **1,800 seconds**, regardless of the selected model. It does
not switch to `jev/auto`, select another provider, edit transcript files, or
change sandbox/approval settings. Existing trusted PreCompact and SessionStart
hooks remain Codex's responsibility; this controller does not bypass trust.

## Terminal launch

From this checkout, create an optional Python environment (Python 3.11+):

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-idle-compact.txt
.venv/bin/python -m harness.idle_compact run
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-idle-compact.txt
.\.venv\Scripts\python.exe -m harness.idle_compact run
```

Set `CODEX_HOME` as usual to choose an installed profile. Codex retains that
profile's model, provider, authentication and reviewed hooks. Pass interactive
Codex arguments after `--`, for example:

```sh
.venv/bin/python -m harness.idle_compact run -- -C /path/to/project -m MODEL_ID
```

`-c` / `--config`, `--enable`, and `--disable` overrides are also forwarded to
the private app-server. This version rejects `--profile`, `--oss` and
`--local-provider` rather than silently applying a different backend profile;
use the intended `CODEX_HOME/config.toml` instead. It also rejects overrides of
the managed `--remote` transport and `--no-daemon`. The launcher requires a
Codex version supporting `--remote` and `thread/backgroundTerminals/list`;
native local acceptance used Codex CLI 0.162.1.

The frontend listens only on an ephemeral `127.0.0.1` port. A random in-memory
bearer token is passed to the native TUI through its environment, not its
arguments or a file. Browser-origin connections and additional clients are
rejected. The backend uses private stdio, so another app-server client cannot
race a new turn against the controller's compact request. This is not isolation
against other processes running as the same OS user.

## Compatible app-server clients

A client that lets you configure its stdio app-server executable can launch:

```sh
python -m harness.idle_compact app-server
```

Run it with the checkout on `PYTHONPATH` or with this checkout as the working
directory. This mode uses only Python's standard library. Native app-server
arguments go after `--`; the controller owns `--listen stdio://`.

This does **not** attach to already-open Codex desktop chats, an existing shared
daemon, or native CanvasTTY sessions. Those clients need an explicit launcher
integration before this feature applies. The normal installer does not replace
their executables, alter installed profiles or enable the timer globally.

## Idle and failure rules

- A thread is eligible after completed work, or a resumed thread with history.
  Empty sessions do not consume compaction calls.
- Active turns, nested workers, outstanding requests/approvals and managed
  processes defer compaction. The controller checks all loaded threads and
  their background terminals before starting. Busy time resets the idle clock.
  Only threads explicitly started/resumed/forked by the client are eligible;
  internally spawned workers are observed for safety, not compacted separately.
- Client activity resets idle time. A return during safety checks cancels that
  attempt. A new request during compaction waits for completion; approval
  replies and an explicit interrupt still pass through.
- One compact is attempted per idle period. Successful new work re-arms it.
  Provider rejection does not trigger a repeating paid retry loop. Compaction
  itself may use model tokens and is lossy, like normal native compaction.
- A request acknowledgement is not completion. Success requires the matching
  `contextCompaction` item and completed native turn. A failed turn is reported
  as failure, not success.
- If the lifecycle cannot be verified within 600 seconds, the owned transport
  closes instead of releasing queued turns into an unknown operation. Resume
  the saved thread normally; queued, unsent input may need to be entered again.
- Unknown/busy loaded threads, nonempty or paginated terminal lists and missing
  safety APIs prevent the attempt. Unsupported clients must not be called
  verified merely because the launcher starts.
  A client opting out of required lifecycle notifications disables the timer;
  its notification preferences are not silently overridden.

The timer uses a monotonic clock while this controller is running. It is not a
background service and cannot act while its process is stopped or the computer
is suspended. Diagnostic JSON on stderr contains event/thread IDs, not prompts,
model responses or tokens. `--idle-seconds` and `--compact-timeout-seconds`
before the Codex `--` separator are intended for bounded testing.

## Verification

```sh
python -m unittest discover -s tests -p 'test_idle_compact*.py' -v
python tests/idle_compact_acceptance.py
# Optional real-terminal acceptance on macOS/Linux (needs websockets):
.venv/bin/python tests/idle_compact_acceptance.py --tui
```

The native fixture creates a fresh temporary profile and serves synthetic model
responses only on loopback. For manual selection and `jev/auto` it requires an
initial turn, one native compact lifecycle, then a completed normal turn in the
same thread with the remembered fact present in the provider input. The timer
is shortened in that fixture; the exact 1,800-second boundary is unit tested
with a fake monotonic clock. These checks do not prove live-provider quality,
installed-profile hook acceptance, a literal 30-minute soak or desktop-client
integration. Consult CI for the actual hosted OS results.

Native protocol reference: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
