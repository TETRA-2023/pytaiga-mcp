"""Session tools: login/logout, session status, current user."""

import logging
import uuid
from typing import Any, Dict, Optional

from src import taiga_client
from src.config import settings
from src.server import app, core
from src.taiga_client import (
    TaigaAuthenticationError,
    TaigaException,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


@app.tool(
    "get_default_session",
    description="Returns the default session ID if auto-authentication from environment variables was successful.",
)
def get_default_session() -> Dict[str, Any]:
    if core.DEFAULT_SESSION_ID in core.active_sessions:
        client = core.active_sessions[core.DEFAULT_SESSION_ID]
        if client and client.is_authenticated:
            return {
                "session_id": core.DEFAULT_SESSION_ID,
                "status": "active",
                "auto_authenticated": True,
            }
    return {
        "status": "unavailable",
        "message": "No default session. Set TAIGA_USERNAME/TAIGA_PASSWORD or call login().",
    }


@app.tool(
    "login",
    description="Log into a Taiga instance. Env vars TAIGA_API_URL, TAIGA_USERNAME, TAIGA_PASSWORD are used as defaults.",
)
def login(
    host: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, str]:
    actual_host = host or settings.host
    actual_username = username or settings.get_username_value()
    actual_password = password or settings.get_password_value()
    if not actual_host:
        raise ValueError("Host required. Set TAIGA_API_URL or pass host parameter.")
    if not actual_username or not actual_password:
        raise ValueError(
            "Credentials required. Set TAIGA_USERNAME/TAIGA_PASSWORD or pass parameters."
        )
    try:
        wrapper = taiga_client.TaigaClientWrapper(host=actual_host)
        if wrapper.login(username=actual_username, password=actual_password):
            session_id = str(uuid.uuid4())
            core.active_sessions[session_id] = wrapper
            if core.DEFAULT_SESSION_ID not in core.active_sessions:
                core.active_sessions[core.DEFAULT_SESSION_ID] = wrapper
            return {"session_id": session_id}
        raise RuntimeError("Login failed.")
    except (ValueError, TaigaException) as e:
        logger.error(f"Login failed: {e}", exc_info=False)
        raise
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}", exc_info=True)
        raise RuntimeError("An unexpected server error occurred during login.")


@app.tool(
    "logout",
    description="Invalidate the current session. Uses the default session if session_id is not provided.",
)
def logout(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    core._drop_session(actual_session_id)
    core._clear_session_cache(actual_session_id)
    return {"status": "logged_out", "session_id": actual_session_id}


@app.tool(
    "session_status",
    description="Check whether the current session is active and which user it belongs to.",
)
def session_status(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core.active_sessions.get(actual_session_id)
    if client and client.is_authenticated:
        try:
            me = client.api.get("/users/me")
            return {
                "status": "active",
                "session_id": actual_session_id,
                "username": me.get("username"),
            }
        except TaigaAuthenticationError:
            # Only a rejected token ends the session; a timeout or 5xx is transient.
            core._drop_session(actual_session_id)
            core._clear_session_cache(actual_session_id)
            return {
                "status": "inactive",
                "reason": "token_invalid",
                "session_id": actual_session_id,
            }
    if client:
        # Session entry exists but lost authentication.
        return {
            "status": "inactive",
            "reason": "not_authenticated",
            "session_id": actual_session_id,
        }
    return {"status": "inactive", "reason": "not_found", "session_id": actual_session_id}


@app.tool(
    "get_current_user",
    description=(
        "Return the identity of the authenticated user for this session. "
        "Useful when the agent needs to resolve 'me' — e.g. 'assign this to me' or 'show my tasks'. "
        "Returns username, display name, email, and user ID."
    ),
)
def get_current_user(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_get_me():
        me = client.api.get("/users/me")
        return {
            "id": me.get("id"),
            "username": me.get("username"),
            "full_name": me.get("full_name_display") or me.get("full_name"),
            "email": me.get("email"),
            "bio": me.get("bio") or None,
            "photo": me.get("photo") or None,
        }

    return core._execute_taiga_operation("get_current_user", do_get_me)
