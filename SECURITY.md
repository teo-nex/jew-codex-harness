# Security policy

This is a local, private alpha. Do not publish vulnerability details, credentials, private prompts, OAuth material, provider configuration, or raw logs in an issue or support bundle. Report a suspected vulnerability privately to the owner of the repository where this project is eventually hosted. This checkout has no configured public security reporting endpoint or response-time promise.

Jev and OmniRoute receive the bounded data needed for routing; the selected model provider receives the full Codex request. Local services must bind only to loopback. The installer refuses foreign services, existing Codex profiles, broad credential permissions, and ambiguous rollback state. On Windows, protected credential ACLs are checked before installation; actual Windows host acceptance remains pending.

See [installation scope](docs/INSTALL.md), [support matrix](docs/SUPPORT.md), and the embedded router's [security model](router/SECURITY.md). Review any diagnostic output before sharing it.
