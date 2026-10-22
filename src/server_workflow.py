"""Compatibility shim: the server moved to the src/server/ package in v3 (B3).

Kept so the `mcp-taiga` / `mcp-taiga-workflow` entry points, `run.sh` and
`import src.server_workflow` keep working while v3 lands. Removed in B10.
"""

import src.server as _server

globals().update({k: v for k, v in vars(_server).items() if not k.startswith("__")})

if __name__ == "__main__":
    main()  # noqa: F821 - provided by the globals() update above
