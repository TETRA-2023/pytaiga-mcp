"""Session registry, per-session cache and the tool error wrapper."""

import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

from src.taiga_client import (
    TaigaAPIError,
    TaigaAuthenticationError,
    TaigaClientWrapper,
    TaigaException,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
# httpx logs every request URL at INFO ("HTTP Request: GET https://...?slug=...").
# taiga_client already logs method, path and status at DEBUG; keep httpx quiet so
# query strings (slugs, refs, filters) don't fill the server log.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

active_sessions: Dict[str, TaigaClientWrapper] = {}
DEFAULT_SESSION_ID = "default"

# Per-session lookup cache: {session_id: {"members_{pid}": [...], ...}}
_session_cache: Dict[str, Dict[str, Any]] = {}


def _session_cache_for(session_id: str) -> Dict[str, Any]:
    if session_id not in _session_cache:
        _session_cache[session_id] = {}
    return _session_cache[session_id]


def _clear_session_cache(session_id: str) -> None:
    _session_cache.pop(session_id, None)


def _drop_session(session_id: str) -> None:
    """Forget a session; close its HTTP pool unless another key still aliases it.

    login() stores the same wrapper under its UUID and, when free, under
    "default", so closing on every pop would break the surviving alias.
    """
    wrapper = active_sessions.pop(session_id, None)
    # list() snapshots the values atomically: tools run in worker threads, and a
    # concurrent login() inserting a key would break a live iteration.
    if wrapper is not None and not any(w is wrapper for w in list(active_sessions.values())):
        wrapper.close()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _get_session_id(session_id: Optional[str] = None) -> str:
    if session_id:
        return session_id
    if DEFAULT_SESSION_ID in active_sessions:
        return DEFAULT_SESSION_ID
    raise ValueError(
        "No session_id provided and no default session available. "
        "Set TAIGA_USERNAME/TAIGA_PASSWORD env vars or call login()."
    )


def _get_authenticated_client(session_id: str) -> TaigaClientWrapper:
    client = active_sessions.get(session_id)
    if not client or not client.is_authenticated:
        raise PermissionError("Invalid or expired session. Please login again.")
    return client


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _execute_taiga_operation(operation_name: str, operation_callable, error_context: str = ""):
    context_str = f" for {error_context}" if error_context else ""
    try:
        return operation_callable()
    except TaigaAPIError as e:
        logger.error(f"Taiga API error in {operation_name}{context_str}: {e}", exc_info=False)
        raise
    except TaigaException as e:
        logger.error(f"Taiga error in {operation_name}{context_str}: {e}", exc_info=False)
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in {operation_name}{context_str}: {e}", exc_info=True)
        raise RuntimeError(f"Server error in {operation_name}: {e}")


# ---------------------------------------------------------------------------
# Session-scoped cache
# ---------------------------------------------------------------------------


def _cached(session_id: str, key: str, factory):
    """Get or lazily compute a session-scoped cache entry.

    Used by the resolvers in resolve.py to avoid re-fetching project lookup tables
    (statuses, members, priorities, …) on every tool call within a session.
    The cache is cleared when the session is logged out.

    Tools run in worker threads, so a logout can clear the cache while this call
    is still computing. The value is stored only if the session and its cache
    dict are still the ones this call started with; otherwise a stale entry
    could be inherited by the next login that takes the same session id.
    """
    cache = _session_cache_for(session_id)
    if key in cache:
        return cache[key]
    wrapper = active_sessions.get(session_id)
    value = factory()
    if active_sessions.get(session_id) is wrapper and _session_cache.get(session_id) is cache:
        cache[key] = value
    return value


@contextmanager
def _admin_required(action: str, project_slug: str):
    """Re-raise Taiga's bare 403 on an admin-only endpoint as an explicit message.

    Taiga answers "You do not have permission to perform this action." On these
    endpoints that usually means "this needs project admin" (the operator
    account, rva, is write-tier), but it is also what a non-member gets, so the
    message names both causes.
    """
    try:
        yield
    except TaigaAuthenticationError as e:
        if e.status_code != 403:
            raise
        raise TaigaAuthenticationError(
            403,
            f"{action} requires project admin rights on '{project_slug}' — or you are not a "
            f"member of it (Taiga: {e.error_detail})",
            e.body,
        ) from e
