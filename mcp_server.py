"""The editor as an MCP server, for Claude Code and Claude Desktop (see README, "## Claude에서 쓰기 (MCP)").

    .venv\\Scripts\\python.exe mcp_server.py      (started by the MCP client, which talks to it over stdio)
"""

import sys

from movie_app.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
