"""The FastMCP instance, its lifespan (env auto-auth), transport selection and main()."""

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

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
        for wrapper in {id(w): w for w in core.active_sessions.values()}.values():
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
