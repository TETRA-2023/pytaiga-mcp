"""pytaiga-mcp server package (v3).

  app.py     FastMCP instance, lifespan (env auto-auth), transport, main()
  core.py    sessions, per-session cache, _execute_taiga_operation
  resolve.py name resolution (project/sprint/status/user/refs) + summaries
  tools/     one module per tool group; importing it registers its tools

Tool modules call shared helpers through their module (`core._cached(...)`,
`resolve._resolve_project(...)`), so one `monkeypatch.setattr(core, ...)`
reaches every caller.
"""

from src.server import app, core, resolve
from src.server.tools import (
    admin,
    attachments,
    bulk,
    comments,
    epics,
    issues,
    items,
    projects,
    session,
    sprints,
    stories,
    tags,
    tasks,
    wiki,
)

# Flat namespace for tests and interactive use: `import src.server as wf;
# wf.<any helper or tool>`. It is a COPY of the names: shared objects
# (active_sessions, _session_cache, mcp) are the same objects, but
# monkeypatch.setattr(wf, ...) reaches nothing — patch the defining module
# (core, resolve, tools.<group>) instead.
for _module in (
    core,
    resolve,
    app,
    session,
    projects,
    stories,
    tasks,
    issues,
    sprints,
    epics,
    items,
    comments,
    wiki,
    attachments,
    tags,
    admin,
    bulk,
):
    globals().update({k: v for k, v in vars(_module).items() if not k.startswith("__")})
del _module
