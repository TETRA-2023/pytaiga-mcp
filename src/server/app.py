"""The FastMCP instance, its lifespan (env auto-auth), transport selection and main()."""

import functools
import inspect
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from mcp.server.fastmcp import FastMCP

from src import taiga_client
from src.config import settings
from src.server import core

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _resolve_transport(argv=None, env=None) -> str:
    if argv is None:
        argv = sys.argv
    if env is None:
        env = dict(os.environ)
    if "--sse" in argv:
        return "sse"
    if "--streamable-http" in argv:
        return "streamable-http"
    env_transport = env.get("TAIGA_TRANSPORT", "").lower()
    if env_transport in VALID_TRANSPORTS:
        return env_transport
    if env_transport:
        logger.warning(
            f"Unknown TAIGA_TRANSPORT '{env_transport}', falling back to stdio. "
            f"Valid: {', '.join(VALID_TRANSPORTS)}"
        )
    return "stdio"


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[None]:
    if settings.has_credentials:
        logger.info("Environment credentials detected. Attempting auto-authentication...")
        try:
            wrapper = taiga_client.TaigaClientWrapper(host=settings.host)
            if wrapper.login(
                username=settings.get_username_value(),
                password=settings.get_password_value(),
            ):
                core.active_sessions[core.DEFAULT_SESSION_ID] = wrapper
                logger.info("Auto-authentication successful.")
        except Exception as e:
            logger.error(f"Auto-authentication error: {e}")
            logger.warning("Continuing without auto-authentication.")
    else:
        logger.info("No environment credentials. Use login() tool.")
    try:
        yield
    finally:
        logger.info("Server shutting down — clearing sessions.")
        for wrapper in {id(w): w for w in list(core.active_sessions.values())}.values():
            wrapper.close()
        core.active_sessions.clear()
        core._session_cache.clear()


_mcp_port_str = os.environ.get("MCP_PORT", "8000")
try:
    _mcp_port = int(_mcp_port_str)
except ValueError:
    logger.error(f"Invalid MCP_PORT '{_mcp_port_str}', falling back to 8000.")
    _mcp_port = 8000

mcp = FastMCP(
    "Taiga Workflow",
    dependencies=["httpx"],
    lifespan=server_lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=_mcp_port,
)


def tool(*args, **kwargs):
    """Register a sync tool on `mcp`, run off the event loop in a worker thread.

    FastMCP calls a sync tool directly on its event loop
    (mcp/server/fastmcp/utilities/func_metadata.py), so one slow Taiga call
    (an unpaginated list, a 30 s timeout) would block every other client of a
    shared HTTP server. Registering an async wrapper that hands the body to
    `anyio.to_thread.run_sync` keeps the loop free.

    - Same tool surface: `functools.wraps` sets `__wrapped__`, and FastMCP
      builds the schema with `inspect.signature`, which follows it.
    - Returns the ORIGINAL sync function, so modules and tests keep calling
      tools synchronously.
    - Thread safety: `httpx.Client` is thread-safe, token refresh is locked
      (TaigaHTTP.refresh), and the shared dicts (active_sessions,
      _session_cache) only see single-key get/set/pop. Two concurrent misses
      on the same cache key may both fetch it; the reads are idempotent.
      Closing a session's client defers until its in-flight requests end
      (TaigaHTTP.close), and _cached never stores into a cache a concurrent
      logout has cleared.
    - Capacity: calls share anyio's default pool of 40 worker threads. A
      cancelled or disconnected call keeps its thread until Taiga answers
      (bounded by the 30 s client timeout). Raise the limit via
      anyio.to_thread.current_default_thread_limiter() if a shared HTTP
      deployment ever needs more concurrency.
    """
    register = mcp.tool(*args, **kwargs)

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):
            # Already async: offloading would hand run_sync a coroutine function
            # and return an un-awaited coroutine as the tool result.
            register(fn)
            return fn

        @functools.wraps(fn)
        async def run_in_worker_thread(*call_args, **call_kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *call_args, **call_kwargs))

        register(run_in_worker_thread)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _warn_legacy_server_mode(env=None) -> None:
    """Warn when a deployment still sets the pre-v3 TAIGA_SERVER_MODE.

    v3 has one server, so the variable no longer selects anything. Start anyway
    instead of failing: a stack still pinned to `full` keeps running (on the
    single server's tools) and the log says why its CRUD tools are gone.
    """
    mode = (os.environ if env is None else env).get("TAIGA_SERVER_MODE", "").strip().lower()
    if mode and mode != "workflow":
        logger.warning(
            f"TAIGA_SERVER_MODE={mode!r} is ignored: the full CRUD server was removed in v3. "
            "Starting the single Taiga MCP server. Unset TAIGA_SERVER_MODE to silence this."
        )


def main() -> None:
    _warn_legacy_server_mode()
    transport = _resolve_transport()
    logger.info(f"Starting Taiga MCP server with {transport} transport")
    mcp.run(transport=transport)
