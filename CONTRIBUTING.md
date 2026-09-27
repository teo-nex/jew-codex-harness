# Contributing

Use synthetic fixtures, keep changes scoped, and report tests actually run. Do not commit `.runtime`, `node_modules`, virtual environments, provider credentials, account lists, local logs, Codex sessions, or copied user prompts.

Offline checks from the repository root:

```sh
python3 -m unittest discover -s tests -q
python3 -m unittest discover -s server -q
(cd router && npm run check)
```

The installer targets a fresh Codex profile. Do not run `install` as a test against an existing profile or active router. `prepare` isolates its temporary Codex profile but writes ignored dependencies into `router/`. Integration acceptance requires a separate profile/host and exact verification of service, client configuration and real `jev/auto` response.

For security findings, follow [SECURITY.md](SECURITY.md). Preserve the upstream MIT notices in `router/` and describe changes to routing, context, auth or rollback with a regression test.
