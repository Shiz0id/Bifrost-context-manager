"""Bifrost MCP launcher — cwd-independent.

`python -m bifrost.mcp_server` only resolves when the working directory is
`tools/`. An MCP client that resolves a relative `cwd` differently, or ignores
it, gets ModuleNotFoundError, the process exits, and the client reports nothing
more useful than "Connection closed" -- which is exactly what happened the first
time this was wired.

This launcher resolves the package from its OWN location, so the server starts
correctly from any working directory and .mcp.json needs no `cwd` at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bifrost.mcp_server import serve  # noqa: E402

if __name__ == "__main__":
    serve()
