# Changelog

## v0.1.0-alpha.1 — 2026-10-26

Experimental source release for the Codex adapter on macOS, Linux and Windows.

- Automatic native routing uses Luna 6 for simple questions and small,
  unambiguous edits, Sol 6.1 for ordinary work, and Astra for 3D, computer use
  and heavy software development. Terra and Sol 6 leave the automatic policy.
- Jev selects reasoning depth with the model; the automatic catalog no longer
  presents a fixed manual effort. Manual models retain their own controls.
- Credential discovery changes take effect in the running router, including
  revocation. This addresses stale discovery state behind some local 401 errors.
- Installation records exact service-file intent before registration, so an
  interrupted owned service install can resume or roll back safely. Changed
  files still require review.
- Gateway preparation selects an installed CPython 3.10–3.14, preferring the
  active PATH interpreter. It avoids an incompatible Windows launcher default;
  uv installations continue to use managed Python 3.12.
- Typed Jev v1 responses accept newer v1 versions and `jev-latest`. Decision
  caches validate typed answers and use atomic replacement; cache-write failures
  do not discard valid decisions.
- Configured provider routing also applies without a prompt-cache key. Route
  leases have absolute time/use bounds, session maps have capacity limits, and
  hook usage logs rotate.
- Hook authorization checks are tightened. Read-only shell calls use the same
  policy across supported tool names. Portable workers reject unsupported
  budget fields instead of silently ignoring them.
- The runtime HTTP dependency is updated to Undici 8.12.3. CI audits runtime
  npm dependencies at the high-severity threshold.
- Sol 6.1 usage remains unpriced until an exact rate card is verified; historical
  model IDs and prices are preserved in existing reports.

### Verification scope

Unit tests and synthetic loopback/client checks validate transport and policy
contracts without real provider requests. GitHub Actions runs the OS matrix;
consult the checks for the release commit for their actual result.

Real-provider quality, total task cost, fresh-host installation/rollback and
Windows desktop authentication remain pending. In particular, this release
does not establish that the reported desktop 401 is resolved: discovery/session
sharing and the desktop client's authorization path must be checked on that
host. Follow [INSTALL.md](docs/INSTALL.md); do not bypass caller authentication.
