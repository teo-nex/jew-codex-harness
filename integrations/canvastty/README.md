# Legacy CanvasTTY bridge adapter

These Python helpers implement the older `/g2/api/*` HTTP bridge and its own
terminal bindings. They are retained for compatibility with that bridge and are
not the integration path for current CanvasTTY sessions.

Inside current CanvasTTY use its `canvastty_agents` MCP tools. CanvasTTY owns
permissions, profiles, task delivery and terminal lifecycle; the harness router
selects model and reasoning depth. Do not silently fall back to this adapter if
a native MCP operation is denied or unavailable.

Standalone `jev-workers` can still reference these helpers when explicitly
configured with the legacy bridge. No legacy bridge is installed by the current
cross-platform installer.
