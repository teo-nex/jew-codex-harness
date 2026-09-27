"""Optional CanvasTTY browser broker configuration for macOS Codex."""

import json
from pathlib import Path
import tomllib


IDENTITY_ENV = (
    "CANVASTTY_AGENT_BROWSER_ADDRESS", "CANVASTTY_AGENT_ID",
    "CANVASTTY_AGENT_CONNECTION_ID", "CANVASTTY_TERMINAL_SESSION_ID",
    "CANVASTTY_AGENT_PROVIDER", "CANVASTTY_AGENT_CAPABILITY",
)
DEFAULT_HELPER = Path("/Applications/CanvasTTY.app/Contents/Resources/agent-browser/mcp-helper.mjs")


def merge_browser_config(source: str, repo: Path, node: str, helper: Path = DEFAULT_HELPER) -> str:
    """Add the broker only when CanvasTTY's helper exists; keep other MCPs."""
    if not helper.is_file():
        return source
    config = tomllib.loads(source)
    current = config.get("mcp_servers", {}).get("jev-browser")
    expected = {
        "command": node,
        "args": [str(repo / "integrations/jev/browser_broker/mcp.mjs")],
        "enabled": True,
        "required": False,
        "env_vars": list(IDENTITY_ENV),
    }
    if current is not None:
        if current != expected:
            raise ValueError("Existing jev-browser MCP differs; migration requires review")
        return source
    lines = ["", "[mcp_servers.jev-browser]", "command = " + json.dumps(node),
             "args = [" + json.dumps(expected["args"][0]) + "]",
             "enabled = true", "required = false",
             "env_vars = [" + ", ".join(json.dumps(value) for value in IDENTITY_ENV) + "]", ""]
    return source.rstrip("\n") + "\n".join(lines)
