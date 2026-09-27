# Continuous integration

GitHub Actions runs offline verification and installation smoke checks across
Ubuntu (`ubuntu-latest`), macOS (`macos-latest`), and Windows (`windows-latest`).
Python tests run on Python 3.11 and 3.13; the embedded router uses Node.js 22.19.

## Workflow lanes

### Python matrix (`python`)
The Python lanes compile `harness`, `tests`, and `server`, then discover unit
tests under `tests/` and `server/` across Ubuntu, macOS, and Windows with Python
3.11 and 3.13. GitHub's `setup-python` supplies Python on Windows, so the same
`python` commands work on every runner; local Windows commands may use `py -3`.

### Router matrix (`router`)
The router lane runs `npm ci`, an isolated Node configuration smoke (`scripts/smoke_config_offline.py`),
the repository's static check (`npm run check`), and focused tests for error-chain,
fetch transport, and transport-failure behavior under Node 22.19 and Python 3.11.
The router lane does not install browser engines, Codex CLI, or provider credentials.

### Install smoke matrix (`install-smoke`)
The dedicated `install-smoke` lane checks installation mechanics across
Ubuntu, macOS, and Windows:

- Pins Python 3.11 and Node.js 22.19+ in job scope.
- Installs the Codex CLI globally via npm (`npm install -g @openai/codex`) so that `codex` is discoverable on `PATH`.
- Executes `python scripts/ci_install_smoke.py` to run `doctor`, `prepare`, `install --dry-run`, the actual `install`, `verify`, and `rollback` in a disposable profile.
- Operates under read-only repository permissions (`contents: read`).
- Uses **no live credentials** and **no repository secrets**. Dependency setup
  still downloads packages from the public registries used by the installer.
- Refuses self-hosted runners. It creates a synthetic key used only for local preflight and runs on a disposable GitHub-hosted VM.

### Repository hygiene (`repository-hygiene`)
The repository-hygiene lane rejects tracked runtime/configuration state,
credential directories and file types, and individual tracked files larger
than 25 MiB. It also builds and verifies the source archive from the checked-out
commit in the runner's temporary directory. Keep generated output, logs, caches,
credentials, and runtime state out of version control. Example configuration files
are allowed when they contain placeholders only.

## Evidence separation: tests vs. installation

Unit tests and isolated configuration smokes prove internal code logic, data transformations,
and deterministic packaging. **Unit tests and smoke tests must never be reported as completed installations.**
Target host verification requires independent client and live acceptance as defined in `AGENT_INSTALL.md`.

### Exact CI proof scope

After a green GitHub-hosted run, CI would prove only the following properties:

1. **Source compilation & test suites:** Python modules compile cleanly without syntax errors; unit tests in `tests/` and `server/` pass on Python 3.11 and 3.13 across Linux, macOS, and Windows.
2. **Router integrity:** Router dependencies install cleanly under Node 22.19; TypeScript/type checks pass; routing and transport failure unit tests pass.
3. **Offline configuration generation:** Offline provider setup, model registry, local transport key generation, and catalog refresh succeed in a temporary, isolated profile directory.
4. **Clean repository hygiene & packaging:** No secrets, credentials, logs, or runtime state are tracked; clean release archives build deterministically.
5. **Install smoke mechanics:** The real installer completes in a fresh profile, owned files and service start, loopback `/health` responds, and owned rollback succeeds on each runner OS. The synthetic key cannot authenticate a live provider request.

The local checkout has no configured Git remote, so writing this workflow does not
count as a GitHub-hosted run. Only a green Actions run for the published commit
is cross-platform installation evidence. A red job is an installation failure
to investigate, not a pass that may be relabeled as a preflight result.

### Unproved live GUI, OAuth, and provider properties

CI **does not** prove and cannot verify the following real-world properties:
1. **Live model providers & gateways:** No live API requests are dispatched to TypeSafe, OpenRouter, or OmniRoute gateways. Live provider availability, decision response quality, latency, quota, and rate limits remain unproved.
2. **OAuth authentication & user login:** No real ChatGPT or Codex OAuth flows are executed. Live browser redirects, token issuance, token storage, and session refresh are neither tested nor proven.
3. **Client GUI & TUI integration:** CI does not launch the interactive Codex application, verify desktop model picker menus, or prove visual presentation.
4. **Hook trust & client loading:** In real environments, Codex requires the operator to manually review and trust non-managed hooks via `/hooks`. CI cannot simulate or bypass this interactive trust boundary.
5. **Live Tool Use & capability intercepts:** PreToolUse and PostToolUse hook receipts against real model output, live file workspace edits, Browser Use, Computer Use, and context compaction carry remain unproved in CI.
6. **Worker orchestration environments:** CI does not run the native CanvasTTY bridge or spawn interactive visible terminal worker windows.
7. **Existing profile migrations:** CI runs only against disposable temporary paths; it does not test or guarantee safe takeover of existing profiles, running daemon services, or port conflicts on user machines.
