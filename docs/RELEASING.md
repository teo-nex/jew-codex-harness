# Release checklist

This repository is a private alpha and has no published release yet.

The agent-first entry point supports an installer agent running in any coding
harness. The only bundled target adapter is Codex. Do not describe the archive
as an automatic Jev installation for every target client. Each target host's
agent must return the evidence levels and outcome from `AGENT_INSTALL.md`.

## Build a local source artifact

The packager reads a committed Git tree with `git archive`; it does not package
uncommitted edits or copy the working directory. Commit the reviewed release
contents first, then run this exact command from the repository root:

```sh
release_commit="$(git rev-parse --short HEAD)"
python3 scripts/package_release.py \
  --output "${TMPDIR:-/tmp}/jev-codex-harness-${release_commit}.tar.gz"
```

The archive must be written outside the repository, or into a Git-ignored
`artifacts/` directory. The command refuses a dirty tree and refuses to replace
an existing archive or sidecar. It includes committed source, the embedded
router, installer, tests, docs, and both license files. It omits runtime/cache
directories and rejects credential-like tracked paths or secret-shaped content.
First-party files are also checked for home-directory paths and email addresses;
operator usage aggregates are excluded. Review docs and behavior manually as
well: automated pattern checks cannot prove that source code reveals no workflow.

The archive contains `RELEASE-MANIFEST.json`, which lists every included file,
size, and SHA-256. Alongside it, the command writes `<archive>.sha256` and
`<archive>.provenance.json` with the source commit, artifact digest, size, and
manifest digest. It verifies member paths, archive size, and each member hash
before reporting success. Rebuilding the same commit with the same packager
version produces identical archive bytes. The package command never uploads,
publishes, installs, or starts services.

The source archive contains no `.git` history. Treat the working repository's
existing history as private until it has been audited separately; distribute
the reviewed archive or initialize a new repository from its extracted files.

Run the focused packager tests with:

```sh
python3 -m unittest tests.test_package_release
```

Before a release or migration: review the exact Git commit, upstream MIT notices, lockfiles, tracked paths and secret scan; run the offline [CI](CI.md) matrix on macOS, Linux and Windows; and perform a fresh-profile install and rollback on each target host. Record process identity, service health, client configuration and one real completed `jev/auto` response separately. Do not call skipped real-host checks passing.

For routing changes, test a failure before client output and an interruption after output; only the former may replay a call. Verify tool-call continuity and that a provider fallback keeps the same Codex dialog. For browser/computer use, test the actual client capability and a denied risky action. Assess model quality on real development tasks separately from transport correctness and estimated API cost.

For the cost display, verify three cases from the installed profile: no routed
calls produce "no data", one native-only `jev/auto` call appears in the report,
and an attempt without usage is marked unpriced. Save a timestamped JSON report
with `api-cost --summary`, then reproduce the same table using `--from-report`.
Publish the coverage line and source-row fingerprint with every screenshot;
the Sol/Astra columns are same-token API-price counterfactuals, not observed
subscription debits or guaranteed savings for another workload.

Publishing to a remote repository or deploying to another machine requires the owner's direction after a reviewable artifact exists. Keep runtime state, keys, logs and OAuth material out of the release archive. A rollback must retain unproven state for inspection rather than remove unrelated data.
