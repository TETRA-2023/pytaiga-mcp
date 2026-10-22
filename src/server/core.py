"""Session registry, per-session cache and the tool error wrapper."""

import logging
from typing import Any, Dict, Optional

from src.taiga_client import (
    TaigaAPIError,
    TaigaClientWrapper,
    TaigaException,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

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
    if wrapper is not None and not any(w is wrapper for w in active_sessions.values()):
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
    """
    cache = _session_cache_for(session_id)
    if key not in cache:
        cache[key] = factory()
    return cache[key]
