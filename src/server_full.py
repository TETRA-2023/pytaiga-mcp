# server.py
import json
import logging
import logging.config
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP
from pytaigaclient.exceptions import TaigaAPIError, TaigaException

from src.config import settings
from src.taiga_client import TaigaClientWrapper, resolve_user_id

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()  # Log to stderr by default
    ],
)
logger = logging.getLogger(__name__)
# Quiet down pytaigaclient library logging if needed
logging.getLogger("pytaigaclient").setLevel(logging.WARNING)

# --- Helper Functions ---


def _parse_mcp_kwargs(kwargs: dict) -> dict:
    """Parse MCP kwargs which may be passed as a JSON string.

    When FastMCP receives **kwargs in a tool function, it may pass
    additional parameters as a JSON string under the 'kwargs' or 'filters' key.
    This function handles that case and returns a proper dict.
    """
    if not kwargs:
        return {}
    # If kwargs contains a single key with a string value, parse it as JSON
    if len(kwargs) == 1:
        key = next(iter(kwargs))
        if key in ("kwargs", "filters"):
            val = kwargs[key]
            if isinstance(val, str):
                if not val:
                    return {}
                try:
                    return json.loads(val)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"Invalid JSON in '{key}' parameter: {e}. "
                        "Please use valid JSON format (e.g., double-quoted strings, no trailing commas)."
                    ) from e
            return val if isinstance(val, dict) else {}
    return kwargs


# --- Kwargs Validation ---
# Allowed kwargs per resource type for security and validation
# Based on Taiga API fields: https://docs.taiga.io/api.html
ALLOWED_KWARGS: Dict[str, set] = {
    "project": {
        "name",
        "is_private",
        "is_featured",
        "description",
        "tags",
        "total_story_points",
        "total_milestones",
        "is_looking_for_people",
        "looking_for_people_note",
        "is_epics_activated",
        "is_backlog_activated",
        "is_kanban_activated",
        "is_wiki_activated",
        "is_issues_activated",
        "videoconferences",
        "videoconferences_extra_data",
        "creation_template",
        "is_contact_activated",
    },
    "user_story": {
        "subject",
        "description",
        "status",
        "is_closed",
        "points",
        "milestone",
        "tags",
        "assigned_to",
        "assigned_users",
        "watchers",
        "client_requirement",
        "team_requirement",
        "is_blocked",
        "blocked_note",
        "backlog_order",
        "sprint_order",
        "kanban_order",
        "due_date",
        "due_date_reason",
        "epics",
        "swimlane",
    },
    "task": {
        "subject",
        "description",
        "status",
        "milestone",
        "user_story",
        "assigned_to",
        "watchers",
        "is_iocaine",
        "tags",
        "is_blocked",
        "blocked_note",
        "due_date",
        "due_date_reason",
        "taskboard_order",
    },
    "issue": {
        "subject",
        "description",
        "status",
        "priority",
        "severity",
        "type",
        "milestone",
        "assigned_to",
        "watchers",
        "tags",
        "is_blocked",
        "blocked_note",
        "due_date",
        "due_date_reason",
    },
    "epic": {
        "subject",
        "description",
        "status",
        "assigned_to",
        "watchers",
        "tags",
        "color",
        "client_requirement",
        "team_requirement",
        "epics_order",
    },
    "milestone": {
        "name",
        "estimated_start",
        "estimated_finish",
        "disponibility",
        "slug",
        "order",
        "watchers",
    },
    "swimlane": {
        "name",
        "order",
    },
    "wiki_page": {
        "slug",
        "content",
    },
}

# --- Comment Type Mapping ---
# Maps user-facing type names to (patch_path_segment, history_path_segment)
_COMMENT_TYPE_MAP = {
    "issue": ("issues", "issue"),
    "task": ("tasks", "task"),
    "user_story": ("userstories", "userstory"),
    "userstory": ("userstories", "userstory"),
    "epic": ("epics", "epic"),
}

# --- History Type Mapping ---
# Maps user-facing type names to history path segments (superset of comment types, includes wiki)
_HISTORY_TYPE_MAP = {
    "issue": "issue",
    "task": "task",
    "user_story": "userstory",
    "userstory": "userstory",
    "epic": "epic",
    "wiki": "wiki",
    "wiki_page": "wiki",
}

# --- Response Field Filtering ---
# Define which fields to include at each verbosity level per resource type
# - 'minimal': Core identification fields only
# - 'standard': Useful fields for typical AI operations (includes 'version' for updates)
# - 'full': None = return all fields (no filtering)
RESPONSE_FIELDS: Dict[str, Dict[str, Optional[List[str]]]] = {
    "project": {
        "minimal": ["id", "name", "slug"],
        "standard": [
            "id",
            "name",
            "slug",
            "description",
            "is_private",
            "tags",
            "created_date",
            "modified_date",
            "version",
        ],
        "full": None,
    },
    "user_story": {
        "minimal": ["id", "ref", "subject", "status", "project"],
        "standard": [
            "id",
            "ref",
            "subject",
            "description",
            "status",
            "status_extra_info",
            "assigned_to",
            "assigned_to_extra_info",
            "milestone",
            "swimlane",
            "project",
            "tags",
            "is_blocked",
            "is_closed",
            "due_date",
            "version",
            "tasks",
        ],
        "full": None,
    },
    "task": {
        "minimal": ["id", "ref", "subject", "status", "project"],
        "standard": [
            "id",
            "ref",
            "subject",
            "description",
            "status",
            "status_extra_info",
            "assigned_to",
            "assigned_to_extra_info",
            "user_story",
            "milestone",
            "project",
            "tags",
            "is_blocked",
            "due_date",
            "version",
        ],
        "full": None,
    },
    "issue": {
        "minimal": ["id", "ref", "subject", "status", "priority", "severity", "project"],
        "standard": [
            "id",
            "ref",
            "subject",
            "description",
            "status",
            "status_extra_info",
            # priority/severity/type are bare integer IDs. Taiga publishes no
            # *_extra_info for them (only assigned_to/owner/project/status), so
            # listing those keys here would be dead config. The `*_name`
            # companions are injected by _annotate_issue_attr_names before
            # filtering; 'minimal' deliberately keeps only the raw IDs.
            "priority",
            "priority_name",
            "severity",
            "severity_name",
            "type",
            "type_name",
            "assigned_to",
            "assigned_to_extra_info",
            "milestone",
            "project",
            "tags",
            "is_blocked",
            "due_date",
            "version",
        ],
        "full": None,
    },
    "epic": {
        "minimal": ["id", "ref", "subject", "status", "project"],
        "standard": [
            "id",
            "ref",
            "subject",
            "description",
            "status",
            "status_extra_info",
            "assigned_to",
            "assigned_to_extra_info",
            "project",
            "tags",
            "color",
            "version",
        ],
        "full": None,
    },
    "milestone": {
        "minimal": ["id", "name", "slug", "project"],
        "standard": [
            "id",
            "name",
            "slug",
            "estimated_start",
            "estimated_finish",
            "closed",
            "project",
            "version",
        ],
        "full": None,
    },
    "swimlane": {
        "minimal": ["id", "name", "project"],
        "standard": ["id", "name", "order", "project"],
        "full": None,
    },
    "member": {
        "minimal": ["id", "user", "full_name"],
        "standard": [
            "id",
            "user",
            "full_name",
            "email",
            "role",
            "role_name",
            "is_admin",
            "project",
        ],
        "full": None,
    },
    "wiki_page": {
        "minimal": ["id", "slug", "project"],
        "standard": ["id", "slug", "content", "project", "version"],
        "full": None,
    },
}

VALID_VERBOSITY_LEVELS = {"minimal", "standard", "full"}


def _validate_kwargs(resource_type: str, kwargs: dict, strict: bool = False) -> dict:
    """Validate kwargs against allowed fields for a resource type.

    Args:
        resource_type: The type of resource (e.g., 'project', 'user_story')
        kwargs: The kwargs dict to validate
        strict: If True, raise ValueError on unexpected kwargs. If False, log and strip.

    Returns:
        Validated kwargs dict with only allowed fields

    Raises:
        ValueError: If strict=True and unexpected kwargs are found
    """
    if not kwargs:
        return {}

    allowed = ALLOWED_KWARGS.get(resource_type)
    if allowed is None:
        # Unknown resource type - pass through but log warning
        logger.warning(f"No kwargs allowlist defined for resource type '{resource_type}'")
        return kwargs

    unexpected = set(kwargs.keys()) - allowed
    if unexpected:
        if strict:
            raise ValueError(
                f"Unexpected kwargs for {resource_type}: {unexpected}. Allowed: {allowed}"
            )
        else:
            logger.warning(f"Stripping unexpected kwargs for {resource_type}: {unexpected}")
            return {k: v for k, v in kwargs.items() if k in allowed}

    return kwargs


# --- Manual Session Management ---
# Store active sessions: session_id -> TaigaClientWrapper instance
active_sessions: Dict[str, TaigaClientWrapper] = {}

# Reserved session ID for auto-authenticated session from environment variables
DEFAULT_SESSION_ID = "default"

# Per-(session, project) cache of issue attribute tables; see _issue_attr_tables.
# Declared up here because _bind_session purges it, and binding happens during
# import-time auto-authentication — before the issue helpers are defined.
# {"<session_id>:<project_id>": {"priority": {id: name}, ...}}
_issue_attr_cache: Dict[str, Dict[str, Dict[int, str]]] = {}


def _purge_issue_attr_cache(session_id: str) -> None:
    """Drop every cached attribute table for a session.

    Keys are ``"<session_id>:<project_id>"``, so one session's entries span every
    project it touched.
    """
    prefix = f"{session_id}:"
    for key in [k for k in _issue_attr_cache if k.startswith(prefix)]:
        del _issue_attr_cache[key]


def _bind_session(session_id: str, wrapper: TaigaClientWrapper) -> None:
    """Register an authenticated client under ``session_id``, clearing stale state.

    Session ids are reusable — ``DEFAULT_SESSION_ID`` is the fixed string "default",
    re-bound by auto-authentication and by ``login`` — so binding a new client to an
    existing id must not leave the previous holder's cached project tables in place.

    This and the two ``_unbind_*`` helpers are the ONLY places that mutate
    ``active_sessions``. Routing every mutation through them keeps "a session and its
    cached tables move together" true by construction rather than by argument — the
    first attempt patched individual call sites and missed one.
    """
    _purge_issue_attr_cache(session_id)
    active_sessions[session_id] = wrapper


def _unbind_session(session_id: str) -> Optional[TaigaClientWrapper]:
    """Remove one session and its cached per-project state; return its client, if any."""
    _purge_issue_attr_cache(session_id)
    return active_sessions.pop(session_id, None)


def _unbind_all_sessions() -> None:
    """Drop every session and all cached per-project state (server shutdown)."""
    active_sessions.clear()
    _issue_attr_cache.clear()


# --- Lifespan for Auto-Authentication ---
@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[None]:
    """
    Manage server startup and shutdown lifecycle.
    Performs auto-authentication if credentials are in environment.
    """
    if settings.has_credentials:
        logger.info("Environment credentials detected. Attempting auto-authentication...")
        try:
            wrapper = TaigaClientWrapper(host=settings.host)
            success = wrapper.login(
                username=settings.get_username_value(), password=settings.get_password_value()
            )
            if success:
                _bind_session(DEFAULT_SESSION_ID, wrapper)
                logger.info(
                    f"Auto-authentication successful. Default session created: '{DEFAULT_SESSION_ID}'"
                )
            else:
                logger.warning("Auto-authentication failed. Manual login required.")
        except Exception as e:
            logger.error(f"Auto-authentication error: {e}")
            logger.warning("Continuing without auto-authentication. Manual login required.")
    else:
        logger.info("No environment credentials found. Manual login required via login() tool.")

    try:
        yield
    finally:
        # Cleanup on shutdown
        logger.info("Server shutting down. Cleaning up sessions...")
        _unbind_all_sessions()


# --- MCP Server Definition ---
_mcp_port_str = os.environ.get("MCP_PORT", "8000")
try:
    _mcp_port = int(_mcp_port_str)
except ValueError:
    logger.error(
        f"Invalid MCP_PORT value '{_mcp_port_str}', must be a number. Falling back to 8000."
    )
    _mcp_port = 8000

mcp = FastMCP(
    "Taiga Bridge",
    dependencies=["pytaigaclient"],
    lifespan=server_lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=_mcp_port,
)

# --- Helper Functions for Session Validation ---


def _get_session_id(session_id: Optional[str] = None) -> str:
    """
    Get session ID, defaulting to 'default' if available.

    Args:
        session_id: Optional explicit session ID

    Returns:
        The session ID to use

    Raises:
        ValueError: If no session_id provided and no default session available
    """
    if session_id:
        return session_id
    if DEFAULT_SESSION_ID in active_sessions:
        return DEFAULT_SESSION_ID
    raise ValueError(
        "No session_id provided and no default session available. "
        "Set TAIGA_USERNAME/TAIGA_PASSWORD environment variables or use login() tool."
    )


def _get_authenticated_client(session_id: str) -> TaigaClientWrapper:
    """
    Retrieves the authenticated TaigaClientWrapper for a given session ID.
    Raises PermissionError if the session is invalid or not found.
    """
    client = active_sessions.get(session_id)
    # Also check if the client object itself exists and is authenticated
    if not client or not client.is_authenticated:
        logger.warning(
            f"Invalid or expired session ID provided: {session_id[:8] if session_id else 'None'}..."
        )
        # Raise PermissionError - FastMCP will map this to an appropriate error response
        raise PermissionError("Invalid or expired session ID. Please login again.")
    logger.debug(f"Retrieved valid client for session ID: {session_id[:8]}...")
    return client


# Load-bearing string: must mirror pytaigaclient's TaigaAPIError default literal
# exactly. If upstream changes the wording, the repair trigger silently no-ops
# in production. The test_repair_taiga_api_error_drf_dict_list test constructs a
# real TaigaAPIError and asserts this string, so a wording drift will fail CI.
_TAIGA_API_ERROR_PLACEHOLDER = "No error message provided by API."


def _repair_taiga_api_error(e: TaigaAPIError) -> None:
    """Rewrite a TaigaAPIError's message in place when pytaigaclient dropped a DRF body.

    pytaigaclient's TaigaAPIError.__init__ only reads the legacy `_error_message`
    key, replacing modern Taiga dict-shaped DRF validation bodies (e.g.
    `{"milestone_id": ["This field is required."]}`) with a placeholder string.
    This helper detects that placeholder, reformats the actual body, and updates
    both `error_detail` and `args` so `str(e)` reflects the repair.

    Dict bodies render as `field: msg1; msg2 | field2: msg`. Nested non-scalar
    values are JSON-encoded so they read as `field: {"k": "v"}` rather than
    Python repr.

    Scope: dict-shaped bodies only. Non-dict bodies (lists, primitives) cannot
    reach this helper as a TaigaAPIError today — pytaigaclient's __init__ calls
    `.get()` on the parsed body and raises AttributeError on lists, so they
    propagate as a different exception class. Adding list handling here would
    be dead code.

    No-op when the detail is not the placeholder, when `e.response` is missing,
    when `.json()` fails, when the body is not a non-empty dict, or when no
    diagnostic content can be extracted. See
    https://github.com/TETRA-2023/pytaiga-mcp/issues/57.
    """
    if getattr(e, "error_detail", None) != _TAIGA_API_ERROR_PLACEHOLDER:
        return
    response = getattr(e, "response", None)
    if response is None:
        return
    try:
        body = response.json()
    except (ValueError, AttributeError, TypeError):
        # ValueError covers stdlib json.JSONDecodeError and requests'
        # JSONDecodeError (both subclasses); AttributeError/TypeError cover
        # response objects whose .json is missing or not callable.
        return
    if not isinstance(body, dict) or not body:
        return
    parts = []
    for k, v in body.items():
        if isinstance(v, list):
            parts.append(f"{k}: {'; '.join(map(str, v))}")
        elif isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}: {json.dumps(v)}")
    new_detail = " | ".join(parts)
    if not new_detail:
        return
    e.error_detail = new_detail
    e.args = (f"API Error {e.status_code}: {new_detail}",)


def _execute_taiga_operation(operation_name: str, operation_callable, error_context: str = ""):
    """
    Execute a Taiga API operation with standardized error handling.

    Args:
        operation_name: Human-readable name of the operation (e.g., "list_projects")
        operation_callable: A callable (lambda or function) that performs the operation
        error_context: Additional context for error messages (e.g., "project 123")

    Returns:
        The result of the operation

    Raises:
        TaigaException: Re-raised from the API. TaigaAPIError messages are repaired
            in-place when the upstream client dropped a DRF-format error body
            (see _repair_taiga_api_error).
        ValueError: Re-raised unwrapped from the operation callable (caller bugs:
            empty kwargs, missing required fields, ref-not-found, etc.) so callers
            see the original message rather than a generic "Server error" wrapper.
        RuntimeError: Wrapped unexpected errors (anything not TaigaException or
            ValueError)
    """
    context_str = f" for {error_context}" if error_context else ""
    try:
        result = operation_callable()
        return result
    except TaigaAPIError as e:
        # Must precede `except TaigaException` — TaigaAPIError is a subclass and
        # Python matches the first compatible except clause.
        _repair_taiga_api_error(e)
        logger.error(f"Taiga API error in {operation_name}{context_str}: {e}", exc_info=False)
        raise e
    except TaigaException as e:
        logger.error(f"Taiga API error in {operation_name}{context_str}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error in {operation_name}{context_str}: {e}", exc_info=True)
        raise RuntimeError(f"Server error in {operation_name}: {e}")


def _filter_response(response, resource_type: str, verbosity: str = "standard"):
    """
    Filter response fields based on verbosity level.

    Args:
        response: API response (dict, list of dicts, or None)
        resource_type: Type of resource (user_story, task, etc.)
        verbosity: One of 'minimal', 'standard', 'full'

    Returns:
        Filtered response with only requested fields

    Note: 'version' is always included in 'standard' level as it's required
    for update operations (optimistic concurrency control).
    """
    if response is None:
        return None

    # Validate verbosity parameter
    if verbosity not in VALID_VERBOSITY_LEVELS:
        logger.warning(f"Invalid verbosity '{verbosity}', using 'standard'")
        verbosity = "standard"

    if verbosity == "full":
        return response

    if resource_type not in RESPONSE_FIELDS:
        logger.debug(f"No filter config for '{resource_type}', returning full response")
        return response

    fields = RESPONSE_FIELDS[resource_type].get(verbosity)
    if fields is None:
        return response

    field_set = set(fields)

    def filter_dict(d: Dict) -> Dict:
        return {k: v for k, v in d.items() if k in field_set}

    if isinstance(response, list):
        return [filter_dict(item) for item in response]
    return filter_dict(response)


def _enrich_user_story_tasks(
    us_result: Dict[str, Any],
    taiga_client_wrapper,
    verbosity: str,
) -> Dict[str, Any]:
    """Enrich a user story response with its associated tasks.

    The Taiga API returns an empty tasks array in user story detail responses.
    This helper fetches the actual tasks and injects them into the response.

    Task verbosity mapping: US standard → task minimal, US full → task standard.

    Note: Mutates us_result in-place (sets the 'tasks' key) and returns it.
    """
    if verbosity == "minimal":
        return us_result

    us_id = us_result.get("id")
    project_id = us_result.get("project")
    if not us_id or not project_id:
        return us_result

    task_verbosity = "minimal" if verbosity == "standard" else "standard"
    try:
        tasks = taiga_client_wrapper.list_resources(
            "tasks", project_id=project_id, user_story=us_id
        )
        us_result["tasks"] = _filter_response(tasks, "task", task_verbosity)
    except Exception as e:
        logger.warning(f"Failed to fetch tasks for user story {us_id}: {e}")
        us_result["tasks"] = []

    return us_result


def _get_item_by_ref(
    item_type: str,
    api_collection_name: str,
    project_id: int,
    ref: int,
    session_id: Optional[str],
    verbosity: str,
) -> Dict[str, Any]:
    """Generic helper to retrieve a Taiga item by its ref number."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_{item_type}_by_ref ref #{ref} in project {project_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_collection = getattr(taiga_client_wrapper.api, api_collection_name)

    item_label = item_type.replace("_", " ")
    result = _execute_taiga_operation(
        f"get_{item_type}_by_ref",
        lambda: api_collection.get_by_ref(ref=ref, project=project_id),
        f"{item_label} ref #{ref} in project {project_id}",
    )
    if not result:
        raise ValueError(
            f"{item_label.capitalize()} with ref #{ref} not found in project {project_id}"
        )
    if item_type == "issue":
        result = _annotate_issue_attr_names(result, actual_session_id, verbosity)
    return _filter_response(result, item_type, verbosity)


# --- Issue attribute name resolution ---
#
# Taiga publishes no priority_extra_info / severity_extra_info / type_extra_info on its
# issue serializer (only assigned_to / owner / project / status), so those three fields
# arrive as bare integer IDs. Workflow mode reverse-maps them to names; full mode is a
# deliberate 1:1 mapping of the REST API, so the raw IDs stay put and we ADD `*_name`
# companions rather than replacing them. That keeps existing callers working while making
# a response like `"priority": 27` interpretable without three extra tool calls.

_ISSUE_ATTR_RESOURCES = {
    "priority": "priorities",
    "severity": "severities",
    "type": "issue_types",
}


def _issue_attr_tables(
    client: TaigaClientWrapper, project_id: int, session_id: str
) -> Dict[str, Dict[int, str]]:
    """Return {attr: {id: name}} for a project's priorities/severities/issue types.

    Cached per (session, project): without this, annotating a list of issues would cost
    three extra API calls per issue. Fetch failures degrade to an empty table so a read
    still succeeds with the raw IDs rather than failing outright.
    """
    key = f"{session_id}:{project_id}"
    tables = _issue_attr_cache.get(key)
    if tables is None:
        tables = {}
        for attr, resource in _ISSUE_ATTR_RESOURCES.items():
            try:
                items = client.list_resources(resource, project_id=project_id)
            except Exception as exc:  # noqa: BLE001 — name lookup is best-effort
                logger.warning(
                    f"Could not load {resource} for project {project_id}; "
                    f"issue {attr} names unavailable: {exc}"
                )
                items = []
            tables[attr] = {
                i["id"]: i.get("name")
                for i in items
                if isinstance(i, dict) and i.get("id") is not None
            }
        _issue_attr_cache[key] = tables
    return tables


def _annotate_issue_attr_names(response, session_id: str, verbosity: str = "standard"):
    """Add priority_name / severity_name / type_name beside the raw integer IDs.

    Accepts a single issue dict or a list of them, and mutates in place (the dicts come
    straight from the API layer). Issues without a ``project`` are skipped — the project
    is what scopes the attribute tables. Must run BEFORE ``_filter_response``, and the
    added keys are allow-listed in ``RESPONSE_FIELDS['issue']['standard']``.

    Skipped entirely at ``verbosity='minimal'``: that tier filters the ``*_name`` keys
    straight back out, so resolving them would spend three API calls per project on data
    guaranteed to be discarded. Any other value (including an invalid one, which
    ``_filter_response`` normalises to 'standard') still resolves.
    """
    if response is None or verbosity == "minimal":
        return response
    items = response if isinstance(response, list) else [response]
    client: Optional[TaigaClientWrapper] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        project_id = item.get("project")
        if project_id is None:
            continue
        if client is None:
            client = _get_authenticated_client(session_id)
        tables = _issue_attr_tables(client, project_id, session_id)
        for attr in _ISSUE_ATTR_RESOURCES:
            attr_id = item.get(attr)
            if attr_id is not None:
                item[f"{attr}_name"] = tables.get(attr, {}).get(attr_id)
    return response


# --- MCP Tools ---


@mcp.tool(
    "get_default_session",
    description="Returns the default session ID if auto-authentication from environment variables was successful.",
)
def get_default_session() -> Dict[str, Any]:
    """
    Returns the default session ID if environment-based authentication was successful.

    Returns:
        Dictionary with session_id if available, or error status.
    """
    if DEFAULT_SESSION_ID in active_sessions:
        client = active_sessions[DEFAULT_SESSION_ID]
        if client and client.is_authenticated:
            return {
                "session_id": DEFAULT_SESSION_ID,
                "status": "active",
                "auto_authenticated": True,
            }
    return {
        "status": "unavailable",
        "message": "No default session. Set TAIGA_USERNAME/TAIGA_PASSWORD environment variables or use login() tool.",
    }


@mcp.tool(
    "login",
    description="Logs into a Taiga instance. Uses environment variables as defaults if parameters not provided.",
)
def login(
    host: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None
) -> Dict[str, str]:
    """
    Handles Taiga login and creates a session.

    Args:
        host: The URL of the Taiga instance. Defaults to TAIGA_API_URL env var.
        username: The Taiga username. Defaults to TAIGA_USERNAME env var.
        password: The Taiga password. Defaults to TAIGA_PASSWORD env var.

    Returns:
        A dictionary containing the session_id upon successful login.
        Example: {"session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
    """
    # Use env vars as defaults
    actual_host = host or settings.host
    actual_username = username or settings.get_username_value()
    actual_password = password or settings.get_password_value()

    if not actual_host:
        raise ValueError("Host URL required. Set TAIGA_API_URL or provide 'host' parameter.")
    if not actual_username or not actual_password:
        raise ValueError(
            "Credentials required. Set TAIGA_USERNAME/TAIGA_PASSWORD or provide parameters."
        )

    logger.info(f"Executing login tool on host '{actual_host}'")

    try:
        wrapper = TaigaClientWrapper(host=actual_host)
        login_successful = wrapper.login(username=actual_username, password=actual_password)

        if login_successful:
            # Generate a unique session ID
            new_session_id = str(uuid.uuid4())
            # Store the authenticated wrapper in our manual session store
            _bind_session(new_session_id, wrapper)
            # Set as default session if none exists yet
            if DEFAULT_SESSION_ID not in active_sessions:
                _bind_session(DEFAULT_SESSION_ID, wrapper)
                logger.info(
                    f"Login successful. Session created and set as default: '{DEFAULT_SESSION_ID}'"
                )
            else:
                logger.info("Login successful. Session created.")
            # Return the session ID to the client
            return {"session_id": new_session_id}
        else:
            # Should not happen if login raises exception on failure, but handle defensively
            logger.error("Login attempt returned False unexpectedly.")
            raise RuntimeError("Login failed for an unknown reason.")

    except (ValueError, TaigaException) as e:
        logger.error(f"Login failed: {e}", exc_info=False)
        # Re-raise the exception - FastMCP will turn it into an error response
        raise e
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}", exc_info=True)
        raise RuntimeError("An unexpected server error occurred during login.")


# --- Project Tools ---


@mcp.tool(
    "list_projects",
    description="Lists projects accessible to the authenticated user. verbosity: 'minimal' (id/name/slug), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_projects(
    session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Lists projects accessible by the authenticated user."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing list_projects for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    result = _execute_taiga_operation(
        "list_projects", lambda: taiga_client_wrapper.list_resources("projects")
    )
    return _filter_response(result, "project", verbosity)


@mcp.tool(
    "list_all_projects",
    description="Lists all projects visible to the user (requires admin privileges for full list). verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_all_projects(
    session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Lists all projects visible to the authenticated user (scope depends on permissions)."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing list_all_projects for session {actual_session_id[:8]}...")
    # pytaigaclient's list() likely behaves similarly to python-taiga's
    return list_projects(actual_session_id, verbosity)  # Keep delegation


@mcp.tool(
    "get_project",
    description="Gets detailed information about a specific project by its ID. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_project(
    project_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves project details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_project ID {project_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_project",
        lambda: taiga_client_wrapper.api.projects.get(project_id),
        f"project {project_id}",
    )
    return _filter_response(result, "project", verbosity)


@mcp.tool(
    "get_project_by_slug",
    description="Gets detailed information about a specific project by its slug. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_project_by_slug(
    slug: str, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves project details by slug."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_project_by_slug '{slug}' for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_project_by_slug",
        lambda: taiga_client_wrapper.api.projects.get_by_slug(slug=slug),
        f"slug '{slug}'",
    )
    return _filter_response(result, "project", verbosity)


@mcp.tool(
    "create_project",
    description=(
        "Creates a new project. Optional fields (e.g. is_private, tags, is_kanban_activated, "
        "is_issues_activated) must be passed as a JSON object via the `kwargs` parameter, NOT "
        "as top-level arguments — top-level args other than the declared signature params are "
        "silently dropped by FastMCP. Allowed keys: see ALLOWED_KWARGS['project'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def create_project(
    name: str,
    description: str,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a new project. Requires name and description. Optional args (e.g., is_private) via kwargs JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("project", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_project '{name}' for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not name or not description:
        raise ValueError("Project name and description are required.")

    result = _execute_taiga_operation(
        "create_project",
        lambda: taiga_client_wrapper.api.projects.create(
            name=name, description=description, **parsed_kwargs
        ),
        f"project '{name}'",
    )
    return _filter_response(result, "project", verbosity)


@mcp.tool(
    "update_project",
    description=(
        "Updates details of an existing project. Pass fields to update as a JSON object via "
        "the `kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Calling with empty `kwargs` "
        "raises ValueError. Allowed keys: see ALLOWED_KWARGS['project'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_project(
    project_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a project. Pass fields to update as kwargs JSON string (e.g., {"name": "New Name", "description": "New Desc"})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("project", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_project ID {project_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        # Use pytaigaclient update pattern: client.resource.update(id=..., data=...)
        if not parsed_kwargs:
            raise ValueError(
                f"update_project called with no fields to update for project {project_id}. "
                'Pass fields inside the `kwargs` JSON object (e.g. kwargs={"name": "..."}), '
                "not as top-level arguments."
            )

        # First fetch the project to get its current version
        current_project = taiga_client_wrapper.api.projects.get(project_id=project_id)
        version = current_project.get("version")
        if not version:
            logger.warning(
                f"Could not determine version for project {project_id}. Attempting update without version."
            )

        # The project update method requires project_id, version, and project_data
        # Use edit() for partial updates (PATCH) instead of update() (PUT)
        updated_project = taiga_client_wrapper.api.projects.edit(
            project_id=project_id, version=version, **parsed_kwargs
        )

        logger.info(f"Project {project_id} update request sent.")
        # Return the result from the update call
        return _filter_response(updated_project, "project", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating project {project_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating project: {e}")


@mcp.tool(
    "delete_project",
    description="Deletes a project by its ID. This is irreversible. Uses default session if session_id not provided.",
)
def delete_project(project_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a project by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_project ID {project_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.projects.delete(project_id=project_id)
        return {"status": "deleted", "project_id": project_id}

    return _execute_taiga_operation("delete_project", do_delete, f"project {project_id}")


# --- Project Tag Management ---


@mcp.tool()
def get_project_tags_colors(
    project_id: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Get the tag-color mapping for a project.

    Args:
        project_id: The project ID
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict mapping tag names to hex color strings (e.g., {'bug': '#FF0000'}).
    """
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_project_tags_colors for project {project_id} "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_get():
        return taiga_client_wrapper.api.get(f"/projects/{project_id}/tags_colors")

    return _execute_taiga_operation("get_project_tags_colors", do_get, f"project {project_id}")


@mcp.tool()
def edit_project_tag(
    project_id: int,
    tag: str,
    color: Optional[str] = None,
    new_tag: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename or recolor a tag in a project.

    Args:
        project_id: The project ID
        tag: The current tag name to edit
        color: New hex color for the tag (e.g., '#FF0000'). Pass None to keep current color.
        new_tag: New name for the tag. Pass None to keep current name.
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the operation with the updated tag details.
    """
    tag = tag.strip() if tag else ""
    if not tag:
        raise ValueError("Tag name cannot be empty.")
    new_tag = new_tag.strip() if new_tag else None
    if color is None and new_tag is None:
        raise ValueError("At least one of 'color' or 'new_tag' must be provided.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing edit_project_tag '{tag}' in project {project_id} "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_edit():
        payload = {"tag": tag}
        if color is not None:
            payload["color"] = color
        if new_tag is not None:
            payload["new_tag"] = new_tag
        taiga_client_wrapper.api.post(f"/projects/{project_id}/edit_tag", json=payload)
        return {
            "status": "tag_updated",
            "project_id": project_id,
            "tag": tag,
            "color": color,
            "new_tag": new_tag,
        }

    return _execute_taiga_operation("edit_project_tag", do_edit, f"project {project_id}")


@mcp.tool()
def mix_project_tags(
    project_id: int,
    from_tags: List[str],
    to_tag: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge multiple tags into a single tag in a project.

    All items tagged with any of the 'from_tags' will be retagged with 'to_tag'.
    The original tags are removed.

    Args:
        project_id: The project ID
        from_tags: List of tag names to merge from
        to_tag: The target tag name to merge into
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the merge operation.
    """
    to_tag = to_tag.strip() if to_tag else ""
    if not to_tag:
        raise ValueError("Target tag name ('to_tag') cannot be empty.")
    if not from_tags or not any(t.strip() for t in from_tags):
        raise ValueError("'from_tags' must contain at least one non-empty tag name.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing mix_project_tags in project {project_id} session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_mix():
        cleaned_from = [t.strip() for t in from_tags if t.strip()]
        payload = {"from_tags": cleaned_from, "to_tag": to_tag}
        taiga_client_wrapper.api.post(f"/projects/{project_id}/mix_tags", json=payload)
        return {
            "status": "tags_merged",
            "project_id": project_id,
            "from_tags": cleaned_from,
            "to_tag": to_tag,
        }

    return _execute_taiga_operation("mix_project_tags", do_mix, f"project {project_id}")


# --- User Story Tools ---
# Note: get_project_roles not implemented - not supported by pytaigaclient


@mcp.tool(
    "list_user_stories",
    description="Lists user stories within a specific project, optionally filtered. Results include both 'id' (internal, use for get/update/delete) and 'ref' (human-readable '#N' shown in Taiga UI). verbosity: 'minimal' (id/ref/subject/status/project), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_user_stories(
    project_id: int,
    filters: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Lists user stories for a project. Optional filters like 'milestone', 'status', 'assigned_to' can be passed as JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_filters = _parse_mcp_kwargs({"filters": filters})
    logger.info(
        f"Executing list_user_stories for project {project_id}, session {actual_session_id[:8]}, filters: {parsed_filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_user_stories",
        lambda: taiga_client_wrapper.list_resources(
            "user_stories", project_id=project_id, **parsed_filters
        ),
        f"project {project_id}",
    )
    return _filter_response(result, "user_story", verbosity)


@mcp.tool(
    "create_user_story",
    description=(
        "Creates a new user story within a project. Optional fields (e.g. description, tags, "
        "status, milestone, assigned_to, points) must be passed as a JSON object via the "
        "`kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Allowed keys: see "
        "ALLOWED_KWARGS['user_story'] in server.py. verbosity: 'minimal', 'standard' (default), "
        "'full'. Uses default session if session_id not provided."
    ),
)
def create_user_story(
    project_id: int,
    subject: str,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a user story. Requires project_id and subject. Optional fields (description, milestone_id, status_id, assigned_to_id, etc.) via kwargs JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("user_story", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_user_story '{subject}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not subject:
        raise ValueError("User story subject cannot be empty.")

    result = _execute_taiga_operation(
        "create_user_story",
        lambda: taiga_client_wrapper.api.user_stories.create(
            project=project_id, subject=subject, **parsed_kwargs
        ),
        f"user story '{subject}'",
    )
    return _filter_response(result, "user_story", verbosity)


@mcp.tool(
    "get_user_story",
    description="Gets detailed information about a specific user story by its internal ID (not the ref number shown in Taiga UI). Use get_user_story_by_ref if you have the '#N' reference number instead. verbosity: 'minimal', 'standard' (default), 'full'. Note: embedded tasks use one verbosity level lower (standard→task minimal with raw status IDs only, full→task standard with status_extra_info). Uses default session if session_id not provided.",
)
def get_user_story(
    user_story_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves user story details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_user_story ID {user_story_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_user_story",
        lambda: taiga_client_wrapper.api.user_stories.get(user_story_id),
        f"user story {user_story_id}",
    )
    filtered = _filter_response(result, "user_story", verbosity)
    return _enrich_user_story_tasks(filtered, taiga_client_wrapper, verbosity)


@mcp.tool(
    "get_user_story_by_ref",
    description="Gets a user story by its human-readable reference number (the '#N' shown in Taiga UI). Requires the project_id. Use this instead of get_user_story when you have a ref number. verbosity: 'minimal', 'standard' (default), 'full'. Note: embedded tasks use one verbosity level lower (standard→task minimal with raw status IDs only, full→task standard with status_extra_info). Uses default session if session_id not provided.",
)
def get_user_story_by_ref(
    project_id: int, ref: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves user story details by ref number within a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_user_story_by_ref ref #{ref} in project {project_id} "
        f"for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_collection = getattr(taiga_client_wrapper.api, "user_stories")

    result = _execute_taiga_operation(
        "get_user_story_by_ref",
        lambda: api_collection.get_by_ref(ref=ref, project=project_id),
        f"user story ref #{ref} in project {project_id}",
    )
    if not result:
        raise ValueError(f"User story with ref #{ref} not found in project {project_id}")
    filtered = _filter_response(result, "user_story", verbosity)
    return _enrich_user_story_tasks(filtered, taiga_client_wrapper, verbosity)


@mcp.tool(
    "update_user_story",
    description=(
        "Updates details of an existing user story. Pass fields to update as a JSON object via "
        'the `kwargs` parameter (e.g. kwargs={"description": "...", "tags": [...], '
        '"status": 2}), NOT as top-level arguments — top-level args other than the declared '
        "signature params are silently dropped by FastMCP. Calling with empty `kwargs` raises "
        "ValueError. Allowed keys: see ALLOWED_KWARGS['user_story'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Note: embedded tasks use one "
        "verbosity level lower (standard→task minimal with raw status IDs only, full→task "
        "standard with status_extra_info). Uses default session if session_id not provided."
    ),
)
def update_user_story(
    user_story_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a user story. Pass fields to update as kwargs JSON string (e.g., {"subject": "New", "status": 2})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("user_story", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_user_story ID {user_story_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        if not parsed_kwargs:
            raise ValueError(
                f"update_user_story called with no fields to update for user story {user_story_id}. "
                "Pass fields inside the `kwargs` JSON object "
                '(e.g. kwargs={"description": "...", "tags": [...]}), not as top-level arguments.'
            )

        # Get current user story data to retrieve version
        current_story = taiga_client_wrapper.api.user_stories.get(user_story_id)
        version = current_story.get("version")
        if not version:
            logger.warning(
                f"Could not determine version for user story {user_story_id}. Attempting update without version."
            )

        # Use edit method for partial updates with keyword arguments
        updated_story = taiga_client_wrapper.api.user_stories.edit(
            user_story_id=user_story_id, version=version, **parsed_kwargs
        )
        logger.info(f"User story {user_story_id} update request sent.")
        return _filter_response(updated_story, "user_story", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating user story {user_story_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating user story {user_story_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating user story: {e}")


@mcp.tool(
    "delete_user_story",
    description="Deletes a user story by its ID. Uses default session if session_id not provided.",
)
def delete_user_story(user_story_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a user story by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_user_story ID {user_story_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.user_stories.delete(user_story_id=user_story_id)
        return {"status": "deleted", "user_story_id": user_story_id}

    return _execute_taiga_operation("delete_user_story", do_delete, f"user story {user_story_id}")


def _resolve_assignee_id(
    user: Union[int, str], resource_type: str, entity_id: int, session_id: str
) -> int:
    """Resolve ``user`` to a Taiga user ID for the assign_* tools.

    Ints pass through unchanged (backward-compatible with the historical
    ``user_id: int`` API — no extra API calls). Strings are treated as an
    email / full name and resolved against the members of the
    entity's project (looked up from the entity), reusing the shared None-safe
    ``resolve_user_id`` so full mode gets the same robust resolution as
    workflow mode (pytaiga-mcp#120).
    """
    if isinstance(user, int) and not isinstance(user, bool):
        return user
    client = _get_authenticated_client(session_id)
    entity = client.get_resource(resource_type, entity_id)
    members = client.list_resources("memberships", project_id=entity.get("project"))
    return resolve_user_id(members, user)


@mcp.tool(
    "assign_user_story_to_user",
    description="Assigns a specific user story to a user. `user` accepts a numeric user ID, or an email or full name (resolved against the project's members; Taiga memberships expose no username). Uses default session if session_id not provided.",
)
def assign_user_story_to_user(
    user_story_id: int, user: Union[int, str], session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Assigns a user story to a user (by ID, email, or full name)."""
    actual_session_id = _get_session_id(session_id)
    user_id = _resolve_assignee_id(user, "user_stories", user_story_id, actual_session_id)
    logger.info(
        f"Executing assign_user_story_to_user: US {user_story_id} -> User {user_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_user_story with assigned_to
    return update_user_story(user_story_id, json.dumps({"assigned_to": user_id}), actual_session_id)


@mcp.tool(
    "unassign_user_story_from_user",
    description="Unassigns a specific user story (sets assigned user to null). Uses default session if session_id not provided.",
)
def unassign_user_story_from_user(
    user_story_id: int, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Unassigns a user story."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing unassign_user_story_from_user: US {user_story_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_user_story with assigned_to=None
    return update_user_story(user_story_id, json.dumps({"assigned_to": None}), actual_session_id)


@mcp.tool(
    "get_user_story_statuses",
    description="Lists the available statuses for user stories within a specific project. Uses default session if session_id not provided.",
)
def get_user_story_statuses(
    project_id: int, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieves the list of user story statuses for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_user_story_statuses for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_user_story_statuses",
        lambda: taiga_client_wrapper.list_resources("userstory_statuses", project_id=project_id),
        f"project {project_id}",
    )


# --- Task Tools ---


@mcp.tool(
    "list_tasks",
    description="Lists tasks within a specific project, optionally filtered. Results include both 'id' (internal, use for get/update/delete) and 'ref' (human-readable '#N' shown in Taiga UI). verbosity: 'minimal' (id/ref/subject/status/project), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_tasks(
    project_id: int,
    filters: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Lists tasks for a project. Optional filters like 'milestone', 'status', 'user_story', 'assigned_to' can be passed as JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_filters = _parse_mcp_kwargs({"filters": filters})
    logger.info(
        f"Executing list_tasks for project {project_id}, session {actual_session_id[:8]}, filters: {parsed_filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_tasks",
        lambda: taiga_client_wrapper.list_resources(
            "tasks", project_id=project_id, **parsed_filters
        ),
        f"project {project_id}",
    )
    return _filter_response(result, "task", verbosity)


@mcp.tool(
    "create_task",
    description=(
        "Creates a new task within a project. Optional fields (e.g. description, tags, status, "
        "milestone, user_story, assigned_to) must be passed as a JSON object via the `kwargs` "
        "parameter, NOT as top-level arguments — top-level args other than the declared "
        "signature params are silently dropped by FastMCP. Allowed keys: see "
        "ALLOWED_KWARGS['task'] in server.py. verbosity: 'minimal', 'standard' (default), "
        "'full'. Uses default session if session_id not provided."
    ),
)
def create_task(
    project_id: int,
    subject: str,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a task. Requires project_id and subject. Optional fields (description, milestone_id, status_id, user_story_id, assigned_to_id, etc.) via kwargs JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("task", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_task '{subject}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not subject:
        raise ValueError("Task subject cannot be empty.")

    result = _execute_taiga_operation(
        "create_task",
        lambda: taiga_client_wrapper.api.tasks.create(
            project=project_id, subject=subject, data=parsed_kwargs if parsed_kwargs else None
        ),
        f"task '{subject}'",
    )
    return _filter_response(result, "task", verbosity)


@mcp.tool(
    "get_task",
    description="Gets detailed information about a specific task by its internal ID (not the ref number shown in Taiga UI). Use get_task_by_ref if you have the '#N' reference number instead. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_task(
    task_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves task details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_task ID {task_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_task", lambda: taiga_client_wrapper.api.tasks.get(task_id), f"task {task_id}"
    )
    return _filter_response(result, "task", verbosity)


@mcp.tool(
    "get_task_by_ref",
    description="Gets a task by its human-readable reference number (the '#N' shown in Taiga UI). Requires the project_id. Use this instead of get_task when you have a ref number. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_task_by_ref(
    project_id: int, ref: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves task details by ref number within a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_task_by_ref ref #{ref} in project {project_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    # Workaround: pytaigaclient Tasks.get_by_ref has a bug - passes query_params but
    # TaigaClient.get expects params. Use the underlying get method directly.
    result = _execute_taiga_operation(
        "get_task_by_ref",
        lambda: taiga_client_wrapper.api.get(
            "/tasks/by_ref", params={"ref": ref, "project": project_id}
        ),
        f"task ref #{ref} in project {project_id}",
    )
    if not result:
        raise ValueError(f"Task with ref #{ref} not found in project {project_id}")
    return _filter_response(result, "task", verbosity)


@mcp.tool(
    "update_task",
    description=(
        "Updates details of an existing task. Pass fields to update as a JSON object via the "
        "`kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Calling with empty `kwargs` "
        "raises ValueError. Allowed keys: see ALLOWED_KWARGS['task'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_task(
    task_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a task. Pass fields to update as kwargs JSON string (e.g., {"subject": "New", "status": 2})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("task", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_task ID {task_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not parsed_kwargs:
            raise ValueError(
                f"update_task called with no fields to update for task {task_id}. "
                "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
            )

        # Get current task data to retrieve version
        current_task = taiga_client_wrapper.api.tasks.get(task_id)
        version = current_task.get("version")
        if not version:
            raise ValueError(f"Could not determine version for task {task_id}")

        # Use edit method for partial updates - pytaigaclient uses data: Dict not **kwargs
        updated_task = taiga_client_wrapper.api.tasks.edit(
            task_id=task_id, version=version, data=parsed_kwargs
        )
        logger.info(f"Task {task_id} update request sent.")
        return _filter_response(updated_task, "task", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating task {task_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating task {task_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating task: {e}")


@mcp.tool(
    "delete_task",
    description="Deletes a task by its ID. Uses default session if session_id not provided.",
)
def delete_task(task_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a task by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(f"Executing delete_task ID {task_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.tasks.delete(task_id=task_id)
        return {"status": "deleted", "task_id": task_id}

    return _execute_taiga_operation("delete_task", do_delete, f"task {task_id}")


@mcp.tool(
    "assign_task_to_user",
    description="Assigns a specific task to a user. `user` accepts a numeric user ID, or an email or full name (resolved against the project's members; Taiga memberships expose no username). Uses default session if session_id not provided.",
)
def assign_task_to_user(
    task_id: int, user: Union[int, str], session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Assigns a task to a user (by ID, email, or full name)."""
    actual_session_id = _get_session_id(session_id)
    user_id = _resolve_assignee_id(user, "tasks", task_id, actual_session_id)
    logger.info(
        f"Executing assign_task_to_user: Task {task_id} -> User {user_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_task with assigned_to
    return update_task(task_id, json.dumps({"assigned_to": user_id}), actual_session_id)


@mcp.tool(
    "unassign_task_from_user",
    description="Unassigns a specific task (sets assigned user to null). Uses default session if session_id not provided.",
)
def unassign_task_from_user(task_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Unassigns a task."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing unassign_task_from_user: Task {task_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_task with assigned_to=None
    return update_task(task_id, json.dumps({"assigned_to": None}), actual_session_id)


@mcp.tool(
    "get_task_statuses",
    description="Lists the available statuses for tasks within a specific project. Uses default session if session_id not provided.",
)
def get_task_statuses(project_id: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves the list of task statuses for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_task_statuses for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_task_statuses",
        lambda: taiga_client_wrapper.list_resources("task_statuses", project_id=project_id),
        f"project {project_id}",
    )


# --- Issue Tools ---


@mcp.tool(
    "list_issues",
    description="Lists issues within a specific project, optionally filtered. Results include both 'id' (internal, use for get/update/delete) and 'ref' (human-readable '#N' shown in Taiga UI). verbosity: 'minimal' (id/ref/subject/status/priority/severity/project), 'standard' (default), 'full'. At 'standard' and 'full' verbosity the response also carries priority_name / severity_name / type_name resolved from those integer IDs (Taiga sends no *_extra_info for them); 'minimal' returns the raw IDs only. Uses default session if session_id not provided.",
)
def list_issues(
    project_id: int,
    filters: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Lists issues for a project. Optional filters like 'milestone', 'status', 'priority', 'severity', 'type', 'assigned_to' can be passed as JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_filters = _parse_mcp_kwargs({"filters": filters})
    logger.info(
        f"Executing list_issues for project {project_id}, session {actual_session_id[:8]}, filters: {parsed_filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_issues",
        lambda: taiga_client_wrapper.list_resources(
            "issues", project_id=project_id, **parsed_filters
        ),
        f"project {project_id}",
    )
    result = _annotate_issue_attr_names(result, actual_session_id, verbosity)
    return _filter_response(result, "issue", verbosity)


@mcp.tool(
    "create_issue",
    description=(
        "Creates a new issue within a project. Optional fields (e.g. description, tags, "
        "milestone, assigned_to) must be passed as a JSON object via the `kwargs` parameter, "
        "NOT as top-level arguments — top-level args other than the declared signature params "
        "are silently dropped by FastMCP. Required fields (priority_id, status_id, severity_id, "
        "type_id) remain top-level. Allowed kwargs keys: see ALLOWED_KWARGS['issue'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def create_issue(
    project_id: int,
    subject: str,
    priority_id: int,
    status_id: int,
    severity_id: int,
    type_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates an issue. Requires project_id, subject, priority_id, status_id, severity_id, type_id. Optional fields (description, assigned_to_id, etc.) via kwargs JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("issue", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_issue '{subject}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not subject:
        raise ValueError("Issue subject cannot be empty.")

    issue_data = {
        "priority": priority_id,
        "status": status_id,
        "type": type_id,
        "severity": severity_id,
        **parsed_kwargs,
    }

    result = _execute_taiga_operation(
        "create_issue",
        lambda: taiga_client_wrapper.api.issues.create(
            project=project_id, subject=subject, data=issue_data
        ),
        f"issue '{subject}'",
    )
    result = _annotate_issue_attr_names(result, actual_session_id, verbosity)
    return _filter_response(result, "issue", verbosity)


@mcp.tool(
    "get_issue",
    description="Gets detailed information about a specific issue by its internal ID (not the ref number shown in Taiga UI). Use get_issue_by_ref if you have the '#N' reference number instead. verbosity: 'minimal', 'standard' (default), 'full'. At 'standard' and 'full' verbosity the response also carries priority_name / severity_name / type_name resolved from those integer IDs (Taiga sends no *_extra_info for them); 'minimal' returns the raw IDs only. Uses default session if session_id not provided.",
)
def get_issue(
    issue_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves issue details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_issue ID {issue_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_issue",
        lambda: taiga_client_wrapper.api.issues.get(issue_id),
        f"issue {issue_id}",
    )
    result = _annotate_issue_attr_names(result, actual_session_id, verbosity)
    return _filter_response(result, "issue", verbosity)


@mcp.tool(
    "get_issue_by_ref",
    description="Gets an issue by its human-readable reference number (the '#N' shown in Taiga UI). Requires the project_id. Use this instead of get_issue when you have a ref number. verbosity: 'minimal', 'standard' (default), 'full'. At 'standard' and 'full' verbosity the response also carries priority_name / severity_name / type_name resolved from those integer IDs (Taiga sends no *_extra_info for them); 'minimal' returns the raw IDs only. Uses default session if session_id not provided.",
)
def get_issue_by_ref(
    project_id: int, ref: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves issue details by ref number within a project."""
    return _get_item_by_ref("issue", "issues", project_id, ref, session_id, verbosity)


@mcp.tool(
    "update_issue",
    description=(
        "Updates details of an existing issue. Pass fields to update as a JSON object via the "
        "`kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Calling with empty `kwargs` "
        "raises ValueError. Allowed keys: see ALLOWED_KWARGS['issue'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_issue(
    issue_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates an issue. Pass fields to update as kwargs JSON string (e.g., {"subject": "New", "status": 2})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("issue", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_issue ID {issue_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not parsed_kwargs:
            raise ValueError(
                f"update_issue called with no fields to update for issue {issue_id}. "
                "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
            )

        # Get current issue data to retrieve version
        current_issue = taiga_client_wrapper.api.issues.get(issue_id)
        version = current_issue.get("version")
        if not version:
            raise ValueError(f"Could not determine version for issue {issue_id}")

        # Use edit method for partial updates - pytaigaclient uses data: Dict not **kwargs
        updated_issue = taiga_client_wrapper.api.issues.edit(
            issue_id=issue_id, version=version, data=parsed_kwargs
        )
        logger.info(f"Issue {issue_id} update request sent.")
        updated_issue = _annotate_issue_attr_names(updated_issue, actual_session_id, verbosity)
        return _filter_response(updated_issue, "issue", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating issue {issue_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating issue {issue_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating issue: {e}")


@mcp.tool(
    "delete_issue",
    description="Deletes an issue by its ID. Uses default session if session_id not provided.",
)
def delete_issue(issue_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes an issue by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(f"Executing delete_issue ID {issue_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.issues.delete(issue_id=issue_id)
        return {"status": "deleted", "issue_id": issue_id}

    return _execute_taiga_operation("delete_issue", do_delete, f"issue {issue_id}")


@mcp.tool(
    "assign_issue_to_user",
    description="Assigns a specific issue to a user. `user` accepts a numeric user ID, or an email or full name (resolved against the project's members; Taiga memberships expose no username). Uses default session if session_id not provided.",
)
def assign_issue_to_user(
    issue_id: int, user: Union[int, str], session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Assigns an issue to a user (by ID, email, or full name)."""
    actual_session_id = _get_session_id(session_id)
    user_id = _resolve_assignee_id(user, "issues", issue_id, actual_session_id)
    logger.info(
        f"Executing assign_issue_to_user: Issue {issue_id} -> User {user_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_issue with assigned_to
    return update_issue(issue_id, json.dumps({"assigned_to": user_id}), actual_session_id)


@mcp.tool(
    "unassign_issue_from_user",
    description="Unassigns a specific issue (sets assigned user to null). Uses default session if session_id not provided.",
)
def unassign_issue_from_user(issue_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Unassigns an issue."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing unassign_issue_from_user: Issue {issue_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_issue with assigned_to=None
    return update_issue(issue_id, json.dumps({"assigned_to": None}), actual_session_id)


@mcp.tool(
    "get_issue_statuses",
    description="Lists the available statuses for issues within a specific project. Uses default session if session_id not provided.",
)
def get_issue_statuses(project_id: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves the list of issue statuses for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_issue_statuses for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_issue_statuses",
        lambda: taiga_client_wrapper.list_resources("issue_statuses", project_id=project_id),
        f"project {project_id}",
    )


@mcp.tool(
    "get_issue_priorities",
    description="Lists the available priorities for issues within a specific project. Uses default session if session_id not provided.",
)
def get_issue_priorities(project_id: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves the list of issue priorities for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_issue_priorities for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_issue_priorities",
        lambda: taiga_client_wrapper.list_resources("priorities", project_id=project_id),
        f"project {project_id}",
    )


@mcp.tool(
    "get_issue_severities",
    description="Lists the available severities for issues within a specific project. Uses default session if session_id not provided.",
)
def get_issue_severities(project_id: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves the list of issue severities for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_issue_severities for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_issue_severities",
        lambda: taiga_client_wrapper.list_resources("severities", project_id=project_id),
        f"project {project_id}",
    )


@mcp.tool(
    "get_issue_types",
    description="Lists the available types for issues within a specific project. Uses default session if session_id not provided.",
)
def get_issue_types(project_id: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves the list of issue types for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_issue_types for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "get_issue_types",
        lambda: taiga_client_wrapper.list_resources("issue_types", project_id=project_id),
        f"project {project_id}",
    )


# --- Project Configuration CRUD Tools ---

# Mapping from user-facing config type to API path segment
_PROJECT_CONFIG_TYPE_MAP = {
    "epic_status": "epic-statuses",
    "userstory_status": "userstory-statuses",
    "user_story_status": "userstory-statuses",
    "task_status": "task-statuses",
    "issue_status": "issue-statuses",
    "issue_type": "issue-types",
    "priority": "priorities",
    "severity": "severities",
}
_PROJECT_CONFIG_VALID_TYPES = [
    "epic_status",
    "issue_status",
    "issue_type",
    "priority",
    "severity",
    "task_status",
    "userstory_status",
]


def _validate_project_config_type(config_type: str) -> str:
    """Validate and normalize a project configuration type."""
    config_type = config_type.strip().lower()
    if config_type not in _PROJECT_CONFIG_TYPE_MAP:
        raise ValueError(
            f"Invalid config_type '{config_type}'. Must be one of: {_PROJECT_CONFIG_VALID_TYPES}"
        )
    return config_type


@mcp.tool(
    "create_project_config",
    description=(
        "Creates a project configuration item (status, type, priority, or severity). "
        "config_type: 'epic_status', 'userstory_status', 'task_status', 'issue_status', "
        "'issue_type', 'priority', or 'severity'. "
        "color is a hex string (e.g. '#999999'). is_closed marks whether the status "
        "represents a closed/done state (statuses only). "
        "Uses default session if session_id not provided."
    ),
)
def create_project_config(
    project_id: int,
    config_type: str,
    name: str,
    color: str = "#999999",
    is_closed: Optional[bool] = None,
    order: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a project configuration item."""
    config_type = _validate_project_config_type(config_type)
    name = name.strip() if name else ""
    if not name:
        raise ValueError("Name cannot be empty.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_project_config '{name}' ({config_type}) in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_path = _PROJECT_CONFIG_TYPE_MAP[config_type]

    def do_create():
        payload: Dict[str, Any] = {
            "project": project_id,
            "name": name,
            "color": color,
        }
        if is_closed is not None:
            payload["is_closed"] = is_closed
        if order is not None:
            payload["order"] = order
        return taiga_client_wrapper.api.post(f"/{api_path}", json=payload)

    return _execute_taiga_operation(
        "create_project_config", do_create, f"'{name}' ({config_type}) in project {project_id}"
    )


@mcp.tool(
    "update_project_config",
    description=(
        "Updates a project configuration item (status, type, priority, or severity). "
        "config_type: 'epic_status', 'userstory_status', 'task_status', 'issue_status', "
        "'issue_type', 'priority', or 'severity'. "
        "item_id is the internal ID of the configuration item. "
        "Uses default session if session_id not provided."
    ),
)
def update_project_config(
    item_id: int,
    config_type: str,
    name: Optional[str] = None,
    color: Optional[str] = None,
    is_closed: Optional[bool] = None,
    order: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Updates a project configuration item."""
    config_type = _validate_project_config_type(config_type)
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Name cannot be empty.")
    if all(v is None for v in [name, color, is_closed, order]):
        raise ValueError("At least one field to update must be provided.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing update_project_config {item_id} ({config_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_path = _PROJECT_CONFIG_TYPE_MAP[config_type]

    def do_update():
        payload: Dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if color is not None:
            payload["color"] = color
        if is_closed is not None:
            payload["is_closed"] = is_closed
        if order is not None:
            payload["order"] = order
        return taiga_client_wrapper.api.patch(f"/{api_path}/{item_id}", json=payload)

    return _execute_taiga_operation("update_project_config", do_update, f"{config_type} {item_id}")


@mcp.tool(
    "delete_project_config",
    description=(
        "Deletes a project configuration item (status, type, priority, or severity). "
        "config_type: 'epic_status', 'userstory_status', 'task_status', 'issue_status', "
        "'issue_type', 'priority', or 'severity'. "
        "item_id is the internal ID of the configuration item. "
        "Uses default session if session_id not provided."
    ),
)
def delete_project_config(
    item_id: int,
    config_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Deletes a project configuration item."""
    config_type = _validate_project_config_type(config_type)

    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_project_config {item_id} ({config_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_path = _PROJECT_CONFIG_TYPE_MAP[config_type]

    def do_delete():
        taiga_client_wrapper.api.delete(f"/{api_path}/{item_id}")
        return {"status": "deleted", "item_id": item_id, "config_type": config_type}

    return _execute_taiga_operation("delete_project_config", do_delete, f"{config_type} {item_id}")


@mcp.tool(
    "bulk_update_order_project_config",
    description=(
        "Bulk updates the display order of project configuration items. "
        "config_type: 'epic_status', 'userstory_status', 'task_status', 'issue_status', "
        "'issue_type', 'priority', or 'severity'. "
        "bulk_orders is a list of [id, order] pairs, e.g. [[1, 1], [2, 2], [3, 3]]. "
        "Uses default session if session_id not provided."
    ),
)
def bulk_update_order_project_config(
    project_id: int,
    config_type: str,
    bulk_orders: List[List[int]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk updates the display order of project configuration items."""
    config_type = _validate_project_config_type(config_type)
    if not bulk_orders:
        raise ValueError("bulk_orders cannot be empty.")
    for pair in bulk_orders:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"Each entry in bulk_orders must be a [id, order] pair, got: {pair}")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_update_order_project_config ({config_type}) in project {project_id}, "
        f"{len(bulk_orders)} items, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    api_path = _PROJECT_CONFIG_TYPE_MAP[config_type]

    def do_bulk_order():
        payload = {"project": project_id, "bulk_orders": bulk_orders}
        taiga_client_wrapper.api.post(f"/{api_path}/bulk_update_order", json=payload)
        return {
            "status": "updated",
            "config_type": config_type,
            "project_id": project_id,
            "items_reordered": len(bulk_orders),
        }

    return _execute_taiga_operation(
        "bulk_update_order_project_config",
        do_bulk_order,
        f"{config_type} in project {project_id}",
    )


# --- Story Points Tools ---


@mcp.tool()
def list_points(
    project_id: int,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List the story point values defined for a project.

    Args:
        project_id: The project ID
        session_id: Optional session ID (uses default if not provided)

    Returns:
        List of point dicts with id, name, value, and order.
    """
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_points for project {project_id} session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    return _execute_taiga_operation(
        "list_points",
        lambda: taiga_client_wrapper.list_resources("points", project_id=project_id),
        f"project {project_id}",
    )


@mcp.tool()
def create_point(
    project_id: int,
    name: str,
    value: Optional[float] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new story point value for a project.

    Args:
        project_id: The project ID
        name: The name/label for the point value (e.g., '1', '2', '3', '5', '8', '?')
        value: Optional numeric value for ordering/calculation. None for non-numeric points.
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict with the created point details.
    """
    name = name.strip() if name else ""
    if not name:
        raise ValueError("Point name cannot be empty.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_point '{name}' in project {project_id} "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_create():
        payload: Dict[str, Any] = {"project": project_id, "name": name}
        if value is not None:
            payload["value"] = value
        return taiga_client_wrapper.api.post("/points", json=payload)

    return _execute_taiga_operation("create_point", do_create, f"project {project_id}")


@mcp.tool()
def update_point(
    point_id: int,
    name: Optional[str] = None,
    value: Optional[float] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing story point value.

    Args:
        point_id: The ID of the point to update
        name: New name for the point value
        value: New numeric value
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict with the updated point details.
    """
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Point name cannot be empty.")
    if name is None and value is None:
        raise ValueError("At least one of 'name' or 'value' must be provided.")

    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing update_point {point_id} session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_update():
        payload: Dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if value is not None:
            payload["value"] = value
        return taiga_client_wrapper.api.patch(f"/points/{point_id}", json=payload)

    return _execute_taiga_operation("update_point", do_update, f"point {point_id}")


@mcp.tool()
def delete_point(
    point_id: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a story point value.

    Args:
        point_id: The ID of the point to delete
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the delete operation.
    """
    actual_session_id = _get_session_id(session_id)
    logger.warning(f"Executing delete_point {point_id} session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.delete(f"/points/{point_id}")
        return {"status": "deleted", "point_id": point_id}

    return _execute_taiga_operation("delete_point", do_delete, f"point {point_id}")


# --- Custom Attributes Tools ---

# Mapping from user-facing entity type to API path segments
_CUSTOM_ATTR_TYPE_MAP = {
    "epic": ("epic-custom-attributes", "epics"),
    "user_story": ("userstory-custom-attributes", "userstories"),
    "userstory": ("userstory-custom-attributes", "userstories"),
    "task": ("task-custom-attributes", "tasks"),
    "issue": ("issue-custom-attributes", "issues"),
}
_CUSTOM_ATTR_VALID_TYPES = ["epic", "issue", "task", "user_story"]


def _validate_custom_attr_type(entity_type: str) -> str:
    """Validate and normalize a custom attribute entity type."""
    entity_type = entity_type.strip().lower()
    if entity_type not in _CUSTOM_ATTR_TYPE_MAP:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {_CUSTOM_ATTR_VALID_TYPES}"
        )
    return entity_type


@mcp.tool(
    "list_custom_attributes",
    description="Lists custom attribute definitions for a project. entity_type: 'epic', 'user_story', 'task', or 'issue'. Uses default session if session_id not provided.",
)
def list_custom_attributes(
    project_id: int,
    entity_type: str,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Lists custom attribute definitions for a given entity type in a project."""
    entity_type = _validate_custom_attr_type(entity_type)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_custom_attributes for {entity_type} in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    attr_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][0]

    return _execute_taiga_operation(
        "list_custom_attributes",
        lambda: taiga_client_wrapper.api.get(f"/{attr_path}", params={"project": project_id}),
        f"{entity_type} in project {project_id}",
    )


@mcp.tool(
    "create_custom_attribute",
    description="Creates a custom attribute definition for a project. entity_type: 'epic', 'user_story', 'task', or 'issue'. type can be 'text', 'multiline', 'richtext', 'date', 'url', 'dropdown', 'checkbox', 'number'. Uses default session if session_id not provided.",
)
def create_custom_attribute(
    project_id: int,
    entity_type: str,
    name: str,
    attr_type: str = "text",
    description: str = "",
    order: Optional[int] = None,
    extra: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a custom attribute definition."""
    entity_type = _validate_custom_attr_type(entity_type)
    name = name.strip() if name else ""
    if not name:
        raise ValueError("Attribute name cannot be empty.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_custom_attribute '{name}' for {entity_type} in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    attr_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][0]

    def do_create():
        payload: Dict[str, Any] = {
            "project": project_id,
            "name": name,
            "type": attr_type,
        }
        if description:
            payload["description"] = description
        if order is not None:
            payload["order"] = order
        if extra is not None:
            payload["extra"] = extra
        return taiga_client_wrapper.api.post(f"/{attr_path}", json=payload)

    return _execute_taiga_operation(
        "create_custom_attribute", do_create, f"'{name}' for {entity_type} in project {project_id}"
    )


@mcp.tool(
    "update_custom_attribute",
    description="Updates a custom attribute definition. entity_type: 'epic', 'user_story', 'task', or 'issue'. Uses default session if session_id not provided.",
)
def update_custom_attribute(
    attribute_id: int,
    entity_type: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    order: Optional[int] = None,
    extra: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Updates a custom attribute definition."""
    entity_type = _validate_custom_attr_type(entity_type)
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Attribute name cannot be empty.")
    if all(v is None for v in [name, description, order, extra]):
        raise ValueError("At least one field to update must be provided.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing update_custom_attribute {attribute_id} ({entity_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    attr_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][0]

    def do_update():
        payload: Dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if order is not None:
            payload["order"] = order
        if extra is not None:
            payload["extra"] = extra
        return taiga_client_wrapper.api.patch(f"/{attr_path}/{attribute_id}", json=payload)

    return _execute_taiga_operation(
        "update_custom_attribute", do_update, f"attribute {attribute_id}"
    )


@mcp.tool(
    "delete_custom_attribute",
    description="Deletes a custom attribute definition. entity_type: 'epic', 'user_story', 'task', or 'issue'. Uses default session if session_id not provided.",
)
def delete_custom_attribute(
    attribute_id: int,
    entity_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Deletes a custom attribute definition."""
    entity_type = _validate_custom_attr_type(entity_type)

    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_custom_attribute {attribute_id} ({entity_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    attr_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][0]

    def do_delete():
        taiga_client_wrapper.api.delete(f"/{attr_path}/{attribute_id}")
        return {"status": "deleted", "attribute_id": attribute_id, "entity_type": entity_type}

    return _execute_taiga_operation(
        "delete_custom_attribute", do_delete, f"attribute {attribute_id}"
    )


@mcp.tool(
    "get_custom_attribute_values",
    description="Gets the custom attribute values for a specific entity (e.g., a user story or task). entity_type: 'epic', 'user_story', 'task', or 'issue'. entity_id is the internal ID of the entity. Returns a dict with 'attributes_values' mapping attribute IDs to their values, plus 'version' for optimistic concurrency. Uses default session if session_id not provided.",
)
def get_custom_attribute_values(
    entity_id: int,
    entity_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Gets custom attribute values for a specific entity."""
    entity_type = _validate_custom_attr_type(entity_type)

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_custom_attribute_values for {entity_type} {entity_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    values_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][1]

    return _execute_taiga_operation(
        "get_custom_attribute_values",
        lambda: taiga_client_wrapper.api.get(
            f"/{values_path}/custom-attributes-values/{entity_id}"
        ),
        f"{entity_type} {entity_id}",
    )


@mcp.tool(
    "set_custom_attribute_values",
    description="Sets custom attribute values for a specific entity. entity_type: 'epic', 'user_story', 'task', or 'issue'. attributes_values is a dict mapping attribute IDs (as strings) to their values. version is required for optimistic concurrency (get it from get_custom_attribute_values). Uses default session if session_id not provided.",
)
def set_custom_attribute_values(
    entity_id: int,
    entity_type: str,
    attributes_values: Dict[str, Any],
    version: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Sets custom attribute values for a specific entity."""
    entity_type = _validate_custom_attr_type(entity_type)
    if not attributes_values:
        raise ValueError("attributes_values cannot be empty.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing set_custom_attribute_values for {entity_type} {entity_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    values_path = _CUSTOM_ATTR_TYPE_MAP[entity_type][1]

    def do_set():
        return taiga_client_wrapper.api.patch(
            f"/{values_path}/custom-attributes-values/{entity_id}",
            json={"attributes_values": attributes_values, "version": version},
        )

    return _execute_taiga_operation(
        "set_custom_attribute_values", do_set, f"{entity_type} {entity_id}"
    )


# --- Attachment Tools ---

# Mapping from user-facing entity type to Taiga attachment API path segment
_ATTACHMENT_TYPE_MAP = {
    "epic": "epics",
    "user_story": "userstories",
    "userstory": "userstories",
    "task": "tasks",
    "issue": "issues",
    "wiki": "wiki",
    "wiki_page": "wiki",
}
_ATTACHMENT_VALID_TYPES = ["epic", "issue", "task", "user_story", "wiki"]


def _validate_attachment_type(entity_type: str) -> str:
    """Validate and normalize an attachment entity type."""
    entity_type = entity_type.strip().lower()
    if entity_type not in _ATTACHMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {_ATTACHMENT_VALID_TYPES}"
        )
    return entity_type


@mcp.tool(
    "list_attachments",
    description="Lists attachments for a specific entity. entity_type: 'epic', 'user_story', 'task', 'issue', or 'wiki'. object_id is the internal ID of the entity. Uses default session if session_id not provided.",
)
def list_attachments(
    object_id: int,
    entity_type: str,
    project_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Lists attachments for a specific entity."""
    entity_type = _validate_attachment_type(entity_type)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_attachments for {entity_type} {object_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    path_segment = _ATTACHMENT_TYPE_MAP[entity_type]

    params: Dict[str, Any] = {"object_id": object_id}
    if project_id is not None:
        params["project"] = project_id

    return _execute_taiga_operation(
        "list_attachments",
        lambda: taiga_client_wrapper.api.get(f"/{path_segment}/attachments", params=params),
        f"{entity_type} {object_id}",
    )


@mcp.tool(
    "get_attachment",
    description="Gets a specific attachment by ID. entity_type: 'epic', 'user_story', 'task', 'issue', or 'wiki'. Uses default session if session_id not provided.",
)
def get_attachment(
    attachment_id: int,
    entity_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Gets a specific attachment by its ID."""
    entity_type = _validate_attachment_type(entity_type)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_attachment {attachment_id} ({entity_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    path_segment = _ATTACHMENT_TYPE_MAP[entity_type]

    return _execute_taiga_operation(
        "get_attachment",
        lambda: taiga_client_wrapper.api.get(f"/{path_segment}/attachments/{attachment_id}"),
        f"attachment {attachment_id}",
    )


@mcp.tool(
    "create_attachment",
    description="Creates an attachment on an entity by uploading a file from the local filesystem. entity_type: 'epic', 'user_story', 'task', 'issue', or 'wiki'. file_path is the absolute path to the file to upload. description is optional. Uses default session if session_id not provided.",
)
def create_attachment(
    project_id: int,
    object_id: int,
    entity_type: str,
    file_path: str,
    description: str = "",
    is_deprecated: bool = False,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates an attachment by uploading a file from the local filesystem."""
    entity_type = _validate_attachment_type(entity_type)
    file_path = file_path.strip() if file_path else ""
    if not file_path:
        raise ValueError("file_path cannot be empty.")
    if not os.path.isfile(file_path):
        raise ValueError(f"File not found: {file_path}")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_attachment on {entity_type} {object_id} in project {project_id}, "
        f"file: {os.path.basename(file_path)}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    path_segment = _ATTACHMENT_TYPE_MAP[entity_type]

    def do_create():
        with open(file_path, "rb") as f:
            files = {"attached_file": (os.path.basename(file_path), f)}
            data = {
                "project": str(project_id),
                "object_id": str(object_id),
            }
            if description:
                data["description"] = description
            if is_deprecated:
                data["is_deprecated"] = "true"
            return taiga_client_wrapper.api.post(
                f"/{path_segment}/attachments", data=data, files=files
            )

    return _execute_taiga_operation(
        "create_attachment",
        do_create,
        f"on {entity_type} {object_id} in project {project_id}",
    )


@mcp.tool(
    "update_attachment",
    description="Updates an attachment's metadata (description, is_deprecated). entity_type: 'epic', 'user_story', 'task', 'issue', or 'wiki'. Uses default session if session_id not provided.",
)
def update_attachment(
    attachment_id: int,
    entity_type: str,
    description: Optional[str] = None,
    is_deprecated: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Updates an attachment's metadata."""
    entity_type = _validate_attachment_type(entity_type)
    if description is None and is_deprecated is None:
        raise ValueError("At least one field to update must be provided.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing update_attachment {attachment_id} ({entity_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    path_segment = _ATTACHMENT_TYPE_MAP[entity_type]

    def do_update():
        payload: Dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if is_deprecated is not None:
            payload["is_deprecated"] = is_deprecated
        return taiga_client_wrapper.api.patch(
            f"/{path_segment}/attachments/{attachment_id}", json=payload
        )

    return _execute_taiga_operation("update_attachment", do_update, f"attachment {attachment_id}")


@mcp.tool(
    "delete_attachment",
    description="Deletes an attachment. entity_type: 'epic', 'user_story', 'task', 'issue', or 'wiki'. Uses default session if session_id not provided.",
)
def delete_attachment(
    attachment_id: int,
    entity_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Deletes an attachment."""
    entity_type = _validate_attachment_type(entity_type)

    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_attachment {attachment_id} ({entity_type}), "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    path_segment = _ATTACHMENT_TYPE_MAP[entity_type]

    def do_delete():
        taiga_client_wrapper.api.delete(f"/{path_segment}/attachments/{attachment_id}")
        return {"status": "deleted", "attachment_id": attachment_id, "entity_type": entity_type}

    return _execute_taiga_operation("delete_attachment", do_delete, f"attachment {attachment_id}")


# --- Epic Tools ---


@mcp.tool(
    "list_epics",
    description="Lists epics within a specific project, optionally filtered. Results include both 'id' (internal, use for get/update/delete) and 'ref' (human-readable '#N' shown in Taiga UI). verbosity: 'minimal' (id/ref/subject/status/project), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_epics(
    project_id: int,
    filters: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Lists epics for a project. Optional filters like 'status', 'assigned_to' can be passed as JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_filters = _parse_mcp_kwargs({"filters": filters})
    logger.info(
        f"Executing list_epics for project {project_id}, session {actual_session_id[:8]}, filters: {parsed_filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_epics",
        lambda: taiga_client_wrapper.list_resources(
            "epics", project_id=project_id, **parsed_filters
        ),
        f"project {project_id}",
    )
    return _filter_response(result, "epic", verbosity)


@mcp.tool(
    "create_epic",
    description=(
        "Creates a new epic within a project. Optional fields (e.g. description, tags, color, "
        "status, assigned_to) must be passed as a JSON object via the `kwargs` parameter, NOT "
        "as top-level arguments — top-level args other than the declared signature params are "
        "silently dropped by FastMCP. Allowed keys: see ALLOWED_KWARGS['epic'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def create_epic(
    project_id: int,
    subject: str,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates an epic. Requires project_id and subject. Optional fields (description, status_id, assigned_to_id, color, etc.) via kwargs JSON string."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("epic", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_epic '{subject}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not subject:
        raise ValueError("Epic subject cannot be empty.")

    result = _execute_taiga_operation(
        "create_epic",
        lambda: taiga_client_wrapper.api.epics.create(
            project=project_id, subject=subject, **parsed_kwargs
        ),
        f"epic '{subject}'",
    )
    return _filter_response(result, "epic", verbosity)


@mcp.tool(
    "get_epic",
    description="Gets detailed information about a specific epic by its internal ID (not the ref number shown in Taiga UI). Use get_epic_by_ref if you have the '#N' reference number instead. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_epic(
    epic_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves epic details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_epic ID {epic_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_epic", lambda: taiga_client_wrapper.api.epics.get(epic_id), f"epic {epic_id}"
    )
    return _filter_response(result, "epic", verbosity)


@mcp.tool(
    "get_epic_by_ref",
    description="Gets an epic by its human-readable reference number (the '#N' shown in Taiga UI). Requires the project_id. Use this instead of get_epic when you have a ref number. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_epic_by_ref(
    project_id: int, ref: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves epic details by ref number within a project."""
    return _get_item_by_ref("epic", "epics", project_id, ref, session_id, verbosity)


@mcp.tool(
    "update_epic",
    description=(
        "Updates details of an existing epic. Pass fields to update as a JSON object via the "
        "`kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Calling with empty `kwargs` "
        "raises ValueError. Allowed keys: see ALLOWED_KWARGS['epic'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_epic(
    epic_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates an epic. Pass fields to update as kwargs JSON string (e.g., {"subject": "New", "color": "#FF0000"})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("epic", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_epic ID {epic_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        if not parsed_kwargs:
            raise ValueError(
                f"update_epic called with no fields to update for epic {epic_id}. "
                "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
            )

        # Get current epic data to retrieve version
        current_epic = taiga_client_wrapper.api.epics.get(epic_id)
        version = current_epic.get("version")
        if not version:
            raise ValueError(f"Could not determine version for epic {epic_id}")

        # Use edit method for partial updates with keyword arguments
        updated_epic = taiga_client_wrapper.api.epics.edit(
            epic_id=epic_id, version=version, **parsed_kwargs
        )
        logger.info(f"Epic {epic_id} update request sent.")
        return _filter_response(updated_epic, "epic", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating epic {epic_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating epic {epic_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating epic: {e}")


@mcp.tool(
    "delete_epic",
    description="Deletes an epic by its ID. Uses default session if session_id not provided.",
)
def delete_epic(epic_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes an epic by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(f"Executing delete_epic ID {epic_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.epics.delete(epic_id=epic_id)
        return {"status": "deleted", "epic_id": epic_id}

    return _execute_taiga_operation("delete_epic", do_delete, f"epic {epic_id}")


@mcp.tool(
    "assign_epic_to_user",
    description="Assigns a specific epic to a user. `user` accepts a numeric user ID, or an email or full name (resolved against the project's members; Taiga memberships expose no username). Uses default session if session_id not provided.",
)
def assign_epic_to_user(
    epic_id: int, user: Union[int, str], session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Assigns an epic to a user (by ID, email, or full name)."""
    actual_session_id = _get_session_id(session_id)
    user_id = _resolve_assignee_id(user, "epics", epic_id, actual_session_id)
    logger.info(
        f"Executing assign_epic_to_user: Epic {epic_id} -> User {user_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_epic with assigned_to
    return update_epic(epic_id, json.dumps({"assigned_to": user_id}), actual_session_id)


@mcp.tool(
    "unassign_epic_from_user",
    description="Unassigns a specific epic (sets assigned user to null). Uses default session if session_id not provided.",
)
def unassign_epic_from_user(epic_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Unassigns an epic."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing unassign_epic_from_user: Epic {epic_id}, session {actual_session_id[:8]}..."
    )
    # Delegate to update_epic with assigned_to=None
    return update_epic(epic_id, json.dumps({"assigned_to": None}), actual_session_id)


@mcp.tool(
    "link_user_story_to_epic",
    description="Links a User Story to an Epic. Uses default session if session_id not provided.",
)
def link_user_story_to_epic(
    epic_id: int, user_story_id: int, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Links a user story to an epic."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing link_user_story_to_epic: Epic {epic_id} <- Story {user_story_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_link():
        # Direct API call to ensure correct endpoint and payload
        # Correct endpoint: /epics/{epic_id}/related_userstories (no underscore in userstories)
        # Payload must include 'epic' ID and 'user_story' ID
        # Using 'json' kwarg as this is standard for requests/client wrappers
        taiga_client_wrapper.api.post(
            f"/epics/{epic_id}/related_userstories",
            json={"epic": epic_id, "user_story": user_story_id},
        )
        return {
            "status": "linked",
            "epic_id": epic_id,
            "user_story_id": user_story_id,
        }

    return _execute_taiga_operation(
        "link_user_story_to_epic", do_link, f"link story {user_story_id} to epic {epic_id}"
    )


# --- Milestone (Sprint) Tools ---


@mcp.tool(
    "list_milestones",
    description="Lists milestones (sprints) within a specific project. verbosity: 'minimal' (id/name/slug/project), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_milestones(
    project_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Lists milestones for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_milestones for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_milestones",
        lambda: taiga_client_wrapper.list_resources("milestones", project_id=project_id),
        f"project {project_id}",
    )
    return _filter_response(result, "milestone", verbosity)


@mcp.tool(
    "create_milestone",
    description="Creates a new milestone (sprint) within a project. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def create_milestone(
    project_id: int,
    name: str,
    estimated_start: str,
    estimated_finish: str,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a milestone. Requires project_id, name, estimated_start (YYYY-MM-DD), and estimated_finish (YYYY-MM-DD)."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_milestone '{name}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not all([name, estimated_start, estimated_finish]):
        raise ValueError("Milestone requires name, estimated_start, and estimated_finish.")

    result = _execute_taiga_operation(
        "create_milestone",
        lambda: taiga_client_wrapper.api.milestones.create(
            project=project_id,
            name=name,
            estimated_start=estimated_start,
            estimated_finish=estimated_finish,
        ),
        f"milestone '{name}'",
    )
    return _filter_response(result, "milestone", verbosity)


@mcp.tool(
    "get_milestone",
    description="Gets detailed information about a specific milestone by its ID. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_milestone(
    milestone_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves milestone details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_milestone ID {milestone_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_milestone",
        lambda: taiga_client_wrapper.api.milestones.get(milestone_id),
        f"milestone {milestone_id}",
    )
    return _filter_response(result, "milestone", verbosity)


@mcp.tool(
    "update_milestone",
    description=(
        "Updates details of an existing milestone. Pass fields to update as a JSON object via "
        "the `kwargs` parameter, NOT as top-level arguments — top-level args other than the "
        "declared signature params are silently dropped by FastMCP. Calling with empty `kwargs` "
        "raises ValueError. Allowed keys: see ALLOWED_KWARGS['milestone'] in server.py. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_milestone(
    milestone_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a milestone. Pass fields to update as kwargs JSON string (e.g., {"name": "Sprint 2", "estimated_finish": "2025-02-28"})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("milestone", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_milestone ID {milestone_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        if not parsed_kwargs:
            raise ValueError(
                f"update_milestone called with no fields to update for milestone {milestone_id}. "
                "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
            )

        # Get current milestone data to retrieve version
        current_milestone = taiga_client_wrapper.api.milestones.get(milestone_id)
        version = current_milestone.get("version")
        if not version:
            logger.warning(
                f"Could not determine version for milestone {milestone_id}. Attempting update without version."
            )

        # Use edit method for partial updates with keyword arguments
        updated_milestone = taiga_client_wrapper.api.milestones.edit(
            milestone_id=milestone_id, version=version, **parsed_kwargs
        )
        logger.info(f"Milestone {milestone_id} update request sent.")
        return _filter_response(updated_milestone, "milestone", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating milestone {milestone_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating milestone {milestone_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating milestone: {e}")


@mcp.tool(
    "delete_milestone",
    description="Deletes a milestone by its ID. Uses default session if session_id not provided.",
)
def delete_milestone(milestone_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a milestone by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_milestone ID {milestone_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.milestones.delete(milestone_id=milestone_id)
        return {"status": "deleted", "milestone_id": milestone_id}

    return _execute_taiga_operation("delete_milestone", do_delete, f"milestone {milestone_id}")


# --- Swimlane Tools ---


@mcp.tool(
    "list_swimlanes",
    description=(
        "Lists kanban swimlanes within a specific project. Swimlanes are horizontal "
        "groupings on the Kanban board (typically aligned with epics, teams, or "
        "initiatives). verbosity: 'minimal' (id/name/project), 'standard' (default — "
        "adds 'order'), 'full' (includes per-swimlane status configuration). Uses "
        "default session if session_id not provided."
    ),
)
def list_swimlanes(
    project_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Lists swimlanes for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_swimlanes for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_swimlanes",
        lambda: taiga_client_wrapper.list_resources("swimlanes", project_id=project_id),
        f"project {project_id}",
    )
    return _filter_response(result, "swimlane", verbosity)


@mcp.tool(
    "create_swimlane",
    description=(
        "Creates a new kanban swimlane within a project. Required: project_id and "
        "name. Taiga auto-assigns 'order' (timestamp-based) and auto-populates "
        "per-swimlane status configuration from the project's user-story statuses. "
        "Note: if this is the first swimlane in the project, Taiga marks it "
        "'default' and the project enters swimlanes-enabled mode; existing user "
        "stories are auto-assigned to that default swimlane. To revert to a "
        "no-swimlanes state, delete all non-default swimlanes first (each with "
        "move_to=<default_id>) then delete the default — Taiga only blocks "
        "deleting the default while other swimlanes still exist. verbosity: "
        "'minimal', 'standard' (default), 'full'. Uses default session if "
        "session_id not provided."
    ),
)
def create_swimlane(
    project_id: int,
    name: str,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a swimlane. Requires project_id and name."""
    if not name:
        raise ValueError("Swimlane requires a non-empty name.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing create_swimlane '{name}' in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_create():
        return taiga_client_wrapper.api.post(
            "/swimlanes",
            json={"project": project_id, "name": name},
        )

    result = _execute_taiga_operation("create_swimlane", do_create, f"swimlane '{name}'")
    return _filter_response(result, "swimlane", verbosity)


@mcp.tool(
    "get_swimlane",
    description=(
        "Gets details about a specific swimlane by its ID, including its per-status "
        "configuration. verbosity: 'minimal', 'standard' (default), 'full' (includes "
        "embedded statuses). Uses default session if session_id not provided."
    ),
)
def get_swimlane(
    swimlane_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves swimlane details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_swimlane ID {swimlane_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_swimlane",
        lambda: taiga_client_wrapper.api.get(f"/swimlanes/{swimlane_id}"),
        f"swimlane {swimlane_id}",
    )
    return _filter_response(result, "swimlane", verbosity)


@mcp.tool(
    "update_swimlane",
    description=(
        "Updates a swimlane's name or order. Pass fields to update as a JSON object "
        "via the `kwargs` parameter, NOT as top-level arguments — top-level args "
        "other than the declared signature params are silently dropped by FastMCP. "
        "Calling with empty `kwargs` raises ValueError. Allowed keys: see "
        "ALLOWED_KWARGS['swimlane'] in server.py (name, order). Swimlanes do not "
        "use optimistic concurrency control — no version field is required. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session "
        "if session_id not provided."
    ),
)
def update_swimlane(
    swimlane_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a swimlane. Pass fields as kwargs JSON (e.g., {"name": "Security"})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("swimlane", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_swimlane ID {swimlane_id} for session {actual_session_id[:8]} "
        f"with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not parsed_kwargs:
        raise ValueError(
            f"update_swimlane called with no fields to update for swimlane {swimlane_id}. "
            "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
        )

    result = _execute_taiga_operation(
        "update_swimlane",
        lambda: taiga_client_wrapper.api.patch(f"/swimlanes/{swimlane_id}", json=parsed_kwargs),
        f"swimlane {swimlane_id}",
    )
    return _filter_response(result, "swimlane", verbosity)


@mcp.tool(
    "delete_swimlane",
    description=(
        "Deletes a swimlane by its ID. User stories are not deleted — they "
        "migrate (see move_to). Two Taiga API constraints to know: (1) the "
        "default swimlane (first one created on the project) cannot be deleted "
        "while other swimlanes still exist — Taiga returns 'The default "
        "swimlane cannot be deleted'. To delete it, first delete all non-default "
        "swimlanes (each with move_to=<default_id>); once the default is the "
        "only one left, deletion succeeds and the project reverts to no-"
        "swimlanes state. (2) Deleting a non-default swimlane that has user "
        "stories assigned requires move_to (an existing swimlane ID in the same "
        "project) to migrate them; without move_to, Taiga rejects with 'Cannot "
        "set swimlane to None if there are available swimlanes'. Uses default "
        "session if session_id not provided."
    ),
)
def delete_swimlane(
    swimlane_id: int,
    move_to: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Deletes a swimlane by ID, optionally migrating its user stories to move_to."""
    if move_to is not None and move_to == swimlane_id:
        raise ValueError(f"move_to ({move_to}) cannot be the same as swimlane_id ({swimlane_id}).")
    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_swimlane ID {swimlane_id} "
        f"(move_to={move_to}) for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        params = {"moveTo": move_to} if move_to is not None else None
        taiga_client_wrapper.api.delete(f"/swimlanes/{swimlane_id}", params=params)
        return {"status": "deleted", "swimlane_id": swimlane_id, "moved_to": move_to}

    return _execute_taiga_operation("delete_swimlane", do_delete, f"swimlane {swimlane_id}")


# --- User Management Tools ---


@mcp.tool(
    "get_project_members",
    description="Lists members of a specific project. verbosity: 'minimal' (id/user/full_name), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_project_members(
    project_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Retrieves the list of members for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_project_members for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_project_members",
        lambda: taiga_client_wrapper.list_resources("memberships", project_id=project_id),
        f"project {project_id}",
    )
    return _filter_response(result, "member", verbosity)


@mcp.tool(
    "invite_project_user",
    description="Invites a user to a project by email with a specific role. Uses default session if session_id not provided.",
)
def invite_project_user(
    project_id: int, email: str, role_id: int, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Invites a user via email to join the project with the specified role ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing invite_project_user {email} to project {project_id} (role {role_id}), session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not email:
        raise ValueError("Email cannot be empty.")

    def do_invite():
        result = taiga_client_wrapper.api.memberships.invite(
            project=project_id, email=email, role_id=role_id
        )
        return (
            result
            if isinstance(result, dict)
            else {"status": "invited", "email": email, "details": result}
        )

    return _execute_taiga_operation(
        "invite_project_user", do_invite, f"email '{email}' to project {project_id}"
    )


# --- Wiki Tools ---


@mcp.tool(
    "list_wiki_pages",
    description="Lists wiki pages within a specific project. verbosity: 'minimal' (id/slug/project), 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def list_wiki_pages(
    project_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> List[Dict[str, Any]]:
    """Lists wiki pages for a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing list_wiki_pages for project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "list_wiki_pages",
        lambda: taiga_client_wrapper.list_resources("wiki", project_id=project_id),
        f"project {project_id}",
    )
    return _filter_response(result, "wiki_page", verbosity)


@mcp.tool(
    "get_wiki_page",
    description="Gets a specific wiki page by its ID. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_wiki_page(
    wiki_page_id: int, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves wiki page details by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing get_wiki_page ID {wiki_page_id} for session {actual_session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_wiki_page",
        lambda: taiga_client_wrapper.api.wiki.get(wiki_page_id),
        f"wiki page {wiki_page_id}",
    )
    return _filter_response(result, "wiki_page", verbosity)


@mcp.tool(
    "create_wiki_page",
    description=(
        "Creates a new wiki page. Required fields (project_id, slug, content) are top-level "
        "params. The `kwargs` parameter accepts only the keys in ALLOWED_KWARGS['wiki_page'] "
        "(currently `slug` and `content`, useful for overriding the positional values via JSON); "
        "any other key is silently dropped by FastMCP if passed at the top level, or stripped by "
        "_validate_kwargs if passed inside kwargs. verbosity: 'minimal', 'standard' (default), "
        "'full'. Uses default session if session_id not provided."
    ),
)
def create_wiki_page(
    project_id: int,
    slug: str,
    content: str,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Creates a wiki page. Requires project_id, slug, and content."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("wiki_page", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing create_wiki_page '{slug}' in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    if not slug or not content:
        raise ValueError("Wiki page slug and content are required.")

    result = _execute_taiga_operation(
        "create_wiki_page",
        lambda: taiga_client_wrapper.api.wiki.create(
            project=project_id, slug=slug, content=content, **parsed_kwargs
        ),
        f"wiki page '{slug}'",
    )
    return _filter_response(result, "wiki_page", verbosity)


@mcp.tool(
    "get_wiki_page_by_slug",
    description="Gets a wiki page by its slug within a project. verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided.",
)
def get_wiki_page_by_slug(
    project_id: int, slug: str, session_id: Optional[str] = None, verbosity: str = "standard"
) -> Dict[str, Any]:
    """Retrieves wiki page details by slug within a project."""
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_wiki_page_by_slug '{slug}' in project {project_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    result = _execute_taiga_operation(
        "get_wiki_page_by_slug",
        lambda: taiga_client_wrapper.api.wiki.get_by_slug(slug=slug, project=project_id),
        f"wiki page slug '{slug}' in project {project_id}",
    )
    if not result:
        raise ValueError(f"Wiki page with slug '{slug}' not found in project {project_id}")
    return _filter_response(result, "wiki_page", verbosity)


@mcp.tool(
    "update_wiki_page",
    description=(
        "Updates an existing wiki page. Pass fields to update as a JSON object via the "
        "`kwargs` parameter (allowed keys for wiki pages: `slug`, `content`), NOT as top-level "
        "arguments — top-level args other than the declared signature params are silently "
        "dropped by FastMCP. Calling with empty `kwargs` raises ValueError. See "
        "ALLOWED_KWARGS['wiki_page'] in server.py for the authoritative list. "
        "verbosity: 'minimal', 'standard' (default), 'full'. Uses default session if session_id not provided."
    ),
)
def update_wiki_page(
    wiki_page_id: int,
    kwargs: Optional[Union[Dict[str, Any], str]] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> Dict[str, Any]:
    """Updates a wiki page. Pass fields to update as kwargs JSON string (e.g., {"content": "New content", "slug": "new-slug"})."""
    actual_session_id = _get_session_id(session_id)
    parsed_kwargs = _validate_kwargs("wiki_page", _parse_mcp_kwargs({"kwargs": kwargs}))
    logger.info(
        f"Executing update_wiki_page ID {wiki_page_id} for session {actual_session_id[:8]} with data: {parsed_kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)
    try:
        if not parsed_kwargs:
            raise ValueError(
                f"update_wiki_page called with no fields to update for wiki page {wiki_page_id}. "
                "Pass fields inside the `kwargs` JSON object, not as top-level arguments."
            )

        # Get current wiki page data to retrieve version
        current_page = taiga_client_wrapper.api.wiki.get(wiki_page_id)
        version = current_page.get("version")
        if not version:
            raise ValueError(f"Could not determine version for wiki page {wiki_page_id}")

        # Use edit method for partial updates
        updated_page = taiga_client_wrapper.api.wiki.edit(
            wiki_page_id=wiki_page_id, version=version, data=parsed_kwargs
        )
        logger.info(f"Wiki page {wiki_page_id} update request sent.")
        return _filter_response(updated_page, "wiki_page", verbosity)
    except TaigaException as e:
        logger.error(f"Taiga API error updating wiki page {wiki_page_id}: {e}", exc_info=False)
        raise e
    except ValueError:
        # Caller-bug ValueErrors (e.g. empty kwargs) propagate without being wrapped.
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating wiki page {wiki_page_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating wiki page: {e}")


@mcp.tool(
    "delete_wiki_page",
    description="Deletes a wiki page by its ID. Uses default session if session_id not provided.",
)
def delete_wiki_page(wiki_page_id: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a wiki page by ID."""
    actual_session_id = _get_session_id(session_id)
    logger.warning(
        f"Executing delete_wiki_page ID {wiki_page_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        taiga_client_wrapper.api.wiki.delete(wiki_page_id=wiki_page_id)
        return {"status": "deleted", "wiki_page_id": wiki_page_id}

    return _execute_taiga_operation("delete_wiki_page", do_delete, f"wiki page {wiki_page_id}")


# --- Session Management Tools ---


@mcp.tool(
    "logout",
    description="Invalidates the current session_id. Uses default session if session_id not provided.",
)
def logout(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Logs out the current session, invalidating the session_id."""
    actual_session_id = _get_session_id(session_id)
    logger.info(f"Executing logout for session {actual_session_id[:8]}...")
    # Remove from dict, return None if not found
    client_wrapper = _unbind_session(actual_session_id)
    if client_wrapper:
        logger.info(f"Session {actual_session_id[:8]} logged out successfully.")
        # No specific API logout call needed usually for token-based auth
        return {"status": "logged_out", "session_id": actual_session_id}
    else:
        logger.warning(f"Attempted to log out non-existent session: {actual_session_id[:8]}")
        return {"status": "session_not_found", "session_id": actual_session_id}


@mcp.tool(
    "session_status",
    description="Checks if the provided session_id is currently active and valid. Uses default session if session_id not provided.",
)
def session_status(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Checks the validity of the current session_id."""
    actual_session_id = _get_session_id(session_id)
    logger.debug(f"Executing session_status check for session {actual_session_id[:8]}...")
    client_wrapper = active_sessions.get(actual_session_id)
    if client_wrapper and client_wrapper.is_authenticated:
        try:
            # Use pytaigaclient users.get_me() call
            me = client_wrapper.api.users.get_me()
            # Extract username from the returned dict
            username = me.get("username", "Unknown")
            logger.debug(f"Session {actual_session_id[:8]} is active for user {username}.")
            return {"status": "active", "session_id": actual_session_id, "username": username}
        except TaigaException:
            logger.warning(
                f"Session {actual_session_id[:8]} found but token seems invalid (API check failed)."
            )
            # Clean up invalid session (and its cached per-project state, or a
            # re-bind of this id would be served these tables).
            _unbind_session(actual_session_id)
            return {
                "status": "inactive",
                "reason": "token_invalid",
                "session_id": actual_session_id,
            }
        except Exception as e:  # Catch broader exceptions during the 'me' call
            logger.error(
                f"Unexpected error during session status check for {actual_session_id[:8]}: {e}",
                exc_info=True,
            )
            # Return a distinct status for unexpected errors during check
            return {"status": "error", "reason": "check_failed", "session_id": actual_session_id}
    elif (
        client_wrapper
    ):  # Client exists but not authenticated (shouldn't happen with current login logic)
        logger.warning(
            f"Session {actual_session_id[:8]} exists but client wrapper is not authenticated."
        )
        return {
            "status": "inactive",
            "reason": "not_authenticated",
            "session_id": actual_session_id,
        }
    else:  # Session ID not found
        logger.debug(f"Session {actual_session_id[:8]} not found.")
        return {"status": "inactive", "reason": "not_found", "session_id": actual_session_id}


# --- Comment Tools ---


@mcp.tool()
def add_comment(
    object_id: int,
    object_type: str,
    comment: str,
    session_id: Optional[str] = None,
) -> dict:
    """Add a comment to a Taiga object (issue, task, user_story, or epic).

    Args:
        object_id: The ID of the object to comment on
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        comment: The comment text to add
        session_id: Optional session ID (uses default if not provided)

    Returns:
        dict with status confirmation
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )
    if not comment or not comment.strip():
        raise ValueError("Comment text must not be empty.")
    # Unescape literal escape sequences that LLMs send through MCP JSON
    comment = comment.replace("\\n", "\n").replace("\\t", "\t")

    patch_path, _ = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_add_comment():
        # Get current version for optimistic concurrency control
        obj = taiga_client_wrapper.api.get(f"/{patch_path}/{object_id}")
        version = obj.get("version")
        if version is None:
            raise ValueError(
                f"Could not determine version for {object_type} {object_id}. Cannot add comment."
            )
        taiga_client_wrapper.api.patch(
            f"/{patch_path}/{object_id}",
            json={"comment": comment, "version": version},
        )
        return {
            "status": "comment_added",
            "object_type": object_type,
            "object_id": object_id,
        }

    return _execute_taiga_operation("add_comment", do_add_comment, f"{object_type} {object_id}")


@mcp.tool()
def list_comments(
    object_id: int,
    object_type: str,
    session_id: Optional[str] = None,
) -> list:
    """List comments on a Taiga object (issue, task, user_story, or epic).

    Args:
        object_id: The ID of the object
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        session_id: Optional session ID (uses default if not provided)

    Returns:
        List of comment dicts with id, comment, comment_html, user, and created_at
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )

    _, history_path = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_list_comments():
        history = taiga_client_wrapper.api.get(f"/history/{history_path}/{object_id}")
        return [
            {
                "id": entry.get("id"),
                "comment": entry["comment"],
                "comment_html": entry.get("comment_html", ""),
                "user": entry.get("user"),
                "created_at": entry.get("created_at"),
            }
            for entry in history
            if entry.get("comment", "").strip() and not entry.get("delete_comment_date")
        ]

    return _execute_taiga_operation("list_comments", do_list_comments, f"{object_type} {object_id}")


@mcp.tool()
def edit_comment(
    object_id: int,
    object_type: str,
    comment_id: str,
    new_comment: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit an existing comment on a Taiga object.

    Args:
        object_id: The ID of the object the comment belongs to
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        comment_id: The ID of the comment to edit
        new_comment: The new comment text
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the edit operation.
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. "
            f"Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )
    new_comment = new_comment.strip() if new_comment else ""
    # Unescape literal escape sequences that LLMs send through MCP JSON
    new_comment = new_comment.replace("\\n", "\n").replace("\\t", "\t")
    if not new_comment:
        raise ValueError("New comment text must not be empty.")
    if not comment_id or not comment_id.strip():
        raise ValueError("Comment ID must not be empty.")

    _, history_path = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_edit():
        # Taiga reads the history entry id from the `id` query string param;
        # only the new comment text goes in the JSON body.
        taiga_client_wrapper.api.post(
            f"/history/{history_path}/{object_id}/edit_comment?id={comment_id}",
            json={"comment": new_comment},
        )
        return {
            "status": "comment_edited",
            "object_type": object_type,
            "object_id": object_id,
            "comment_id": comment_id,
        }

    return _execute_taiga_operation(
        "edit_comment", do_edit, f"{object_type} {object_id} comment {comment_id}"
    )


@mcp.tool()
def delete_comment(
    object_id: int,
    object_type: str,
    comment_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Soft-delete a comment on a Taiga object. Can be restored with undelete_comment.

    Args:
        object_id: The ID of the object the comment belongs to
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        comment_id: The ID of the comment to delete
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the delete operation.
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. "
            f"Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )
    if not comment_id or not comment_id.strip():
        raise ValueError("Comment ID must not be empty.")

    _, history_path = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_delete():
        # Taiga reads the history entry id from the `id` query string param;
        # the endpoint has no JSON body.
        taiga_client_wrapper.api.post(
            f"/history/{history_path}/{object_id}/delete_comment?id={comment_id}",
        )
        return {
            "status": "comment_deleted",
            "object_type": object_type,
            "object_id": object_id,
            "comment_id": comment_id,
        }

    return _execute_taiga_operation(
        "delete_comment", do_delete, f"{object_type} {object_id} comment {comment_id}"
    )


@mcp.tool()
def undelete_comment(
    object_id: int,
    object_type: str,
    comment_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore a previously soft-deleted comment on a Taiga object.

    Args:
        object_id: The ID of the object the comment belongs to
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        comment_id: The ID of the comment to restore
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict confirming the restore operation.
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. "
            f"Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )
    if not comment_id or not comment_id.strip():
        raise ValueError("Comment ID must not be empty.")

    _, history_path = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_undelete():
        # Taiga reads the history entry id from the `id` query string param;
        # the endpoint has no JSON body.
        taiga_client_wrapper.api.post(
            f"/history/{history_path}/{object_id}/undelete_comment?id={comment_id}",
        )
        return {
            "status": "comment_restored",
            "object_type": object_type,
            "object_id": object_id,
            "comment_id": comment_id,
        }

    return _execute_taiga_operation(
        "undelete_comment", do_undelete, f"{object_type} {object_id} comment {comment_id}"
    )


@mcp.tool()
def get_comment_versions(
    object_id: int,
    object_type: str,
    comment_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Get the edit history (versions) of a comment on a Taiga object.

    Args:
        object_id: The ID of the object the comment belongs to
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory', or 'epic'
        comment_id: The ID of the comment
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict with 'comment_id' and 'versions' list containing past versions of the comment.
    """
    if object_type not in _COMMENT_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. "
            f"Must be one of: {', '.join(sorted(_COMMENT_TYPE_MAP.keys()))}"
        )
    if not comment_id or not comment_id.strip():
        raise ValueError("Comment ID must not be empty.")

    _, history_path = _COMMENT_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_get_versions():
        # Taiga reads the history entry id from the `id` query string param.
        result = taiga_client_wrapper.api.get(
            f"/history/{history_path}/{object_id}/comment_versions?id={comment_id}"
        )
        return {
            "object_type": object_type,
            "object_id": object_id,
            "comment_id": comment_id,
            "versions": result,
        }

    return _execute_taiga_operation(
        "get_comment_versions",
        do_get_versions,
        f"{object_type} {object_id} comment {comment_id}",
    )


# --- History / Audit Trail ---


@mcp.tool()
def get_history(
    object_id: int,
    object_type: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Get the full change history (audit trail) for a Taiga object.

    Returns all history entries including field changes, status transitions,
    assignments, and comments.

    Args:
        object_id: The ID of the object
        object_type: Type of object: 'issue', 'task', 'user_story', 'userstory',
                     'epic', 'wiki', or 'wiki_page'
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict with 'object_type', 'object_id', and 'history' list of change entries.
    """
    if object_type not in _HISTORY_TYPE_MAP:
        raise ValueError(
            f"Invalid object_type '{object_type}'. "
            f"Must be one of: {', '.join(sorted(_HISTORY_TYPE_MAP.keys()))}"
        )

    history_path = _HISTORY_TYPE_MAP[object_type]
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing get_history for {object_type} {object_id} session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_get_history():
        result = taiga_client_wrapper.api.get(f"/history/{history_path}/{object_id}")
        return {
            "object_type": object_type,
            "object_id": object_id,
            "history": result if isinstance(result, list) else [],
        }

    return _execute_taiga_operation("get_history", do_get_history, f"{object_type} {object_id}")


# --- Bulk Operations ---


def _clean_subjects(subjects: List[str]) -> List[str]:
    """Validate and clean a list of subject strings for bulk creation."""
    if not subjects:
        raise ValueError("Subjects list cannot be empty.")
    cleaned = [s.strip() for s in subjects if s and s.strip()]
    if not cleaned:
        raise ValueError("Subjects list contains only empty strings.")
    return cleaned


@mcp.tool(
    "bulk_create_user_stories",
    description="Creates multiple user stories at once from a list of subjects. All stories are created in the specified project. Returns the list of created stories. Uses default session if session_id not provided.",
)
def bulk_create_user_stories(
    project_id: int,
    subjects: List[str],
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Bulk-creates user stories from a list of subject strings."""
    cleaned = _clean_subjects(subjects)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_create_user_stories: {len(cleaned)} stories in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        bulk_stories = "\n".join(cleaned)
        result = taiga_client_wrapper.api.post(
            "/userstories/bulk_create",
            json={"project_id": project_id, "bulk_stories": bulk_stories},
        )
        return result if isinstance(result, list) else []

    result = _execute_taiga_operation(
        "bulk_create_user_stories", do_bulk, f"{len(cleaned)} stories in project {project_id}"
    )
    return _filter_response(result, "user_story", verbosity)


@mcp.tool(
    "bulk_create_tasks",
    description=(
        "Creates multiple tasks at once from a list of subjects within a sprint. "
        "All tasks are created in the specified project and milestone (sprint), "
        "optionally linked to a user story. Note: Taiga's /tasks/bulk_create "
        "endpoint is sprint-scoped — milestone_id is required by the Taiga API "
        "(verified on Taiga 2.40+) regardless of project settings. Kanban-only "
        "projects (backlog not activated) cannot use this endpoint at all; fall "
        "back to individual create_task calls in a loop. If user_story_id is "
        "provided, the user story must belong to the specified milestone — "
        "Taiga validates this alignment and rejects mismatches with 'Invalid "
        "user story id'. Returns the list of created tasks. Uses default "
        "session if session_id not provided."
    ),
)
def bulk_create_tasks(
    project_id: int,
    subjects: List[str],
    milestone_id: Optional[int] = None,
    user_story_id: Optional[int] = None,
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Bulk-creates tasks from a list of subject strings within a sprint."""
    cleaned = _clean_subjects(subjects)
    if milestone_id is None:
        raise ValueError(
            "milestone_id is required by Taiga's /tasks/bulk_create endpoint "
            "(verified on Taiga 2.40+). The endpoint is sprint-scoped — there "
            "is no API mode that bulk-creates tasks outside a milestone. "
            "For Kanban-only projects (backlog not activated, no milestones), "
            "fall back to individual create_task calls in a loop."
        )
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_create_tasks: {len(cleaned)} tasks in project {project_id}, "
        f"milestone {milestone_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        bulk_tasks = "\n".join(cleaned)
        payload: Dict[str, Any] = {
            "project_id": project_id,
            "bulk_tasks": bulk_tasks,
            "milestone_id": milestone_id,
        }
        if user_story_id is not None:
            payload["us_id"] = user_story_id
        result = taiga_client_wrapper.api.post("/tasks/bulk_create", json=payload)
        return result if isinstance(result, list) else []

    result = _execute_taiga_operation(
        "bulk_create_tasks", do_bulk, f"{len(cleaned)} tasks in project {project_id}"
    )
    return _filter_response(result, "task", verbosity)


@mcp.tool(
    "bulk_create_issues",
    description="Creates multiple issues at once from a list of subjects. All issues are created in the specified project. Returns the list of created issues. Uses default session if session_id not provided.",
)
def bulk_create_issues(
    project_id: int,
    subjects: List[str],
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Bulk-creates issues from a list of subject strings."""
    cleaned = _clean_subjects(subjects)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_create_issues: {len(cleaned)} issues in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        bulk_issues = "\n".join(cleaned)
        result = taiga_client_wrapper.api.post(
            "/issues/bulk_create",
            json={"project_id": project_id, "bulk_issues": bulk_issues},
        )
        return result if isinstance(result, list) else []

    result = _execute_taiga_operation(
        "bulk_create_issues", do_bulk, f"{len(cleaned)} issues in project {project_id}"
    )
    result = _annotate_issue_attr_names(result, actual_session_id, verbosity)
    return _filter_response(result, "issue", verbosity)


@mcp.tool(
    "bulk_create_epics",
    description="Creates multiple epics at once from a list of subjects. All epics are created in the specified project. Returns the list of created epics. Uses default session if session_id not provided.",
)
def bulk_create_epics(
    project_id: int,
    subjects: List[str],
    session_id: Optional[str] = None,
    verbosity: str = "standard",
) -> List[Dict[str, Any]]:
    """Bulk-creates epics from a list of subject strings."""
    cleaned = _clean_subjects(subjects)
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_create_epics: {len(cleaned)} epics in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        bulk_epics = "\n".join(cleaned)
        result = taiga_client_wrapper.api.post(
            "/epics/bulk_create",
            json={"project_id": project_id, "bulk_epics": bulk_epics},
        )
        return result if isinstance(result, list) else []

    result = _execute_taiga_operation(
        "bulk_create_epics", do_bulk, f"{len(cleaned)} epics in project {project_id}"
    )
    return _filter_response(result, "epic", verbosity)


@mcp.tool(
    "bulk_update_user_story_milestone",
    description="Moves multiple user stories to a sprint (milestone) in a single call. Requires project_id, milestone_id, and a list of user story IDs (bulk_stories). Uses default session if session_id not provided.",
)
def bulk_update_user_story_milestone(
    project_id: int,
    milestone_id: int,
    bulk_stories: List[Dict[str, int]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Moves multiple user stories to a milestone. bulk_stories is a list of {us_id, order} dicts."""
    if not bulk_stories:
        raise ValueError("bulk_stories list cannot be empty.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_update_user_story_milestone: {len(bulk_stories)} stories -> "
        f"milestone {milestone_id} in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        taiga_client_wrapper.api.post(
            "/userstories/bulk_update_milestone",
            json={
                "project_id": project_id,
                "milestone_id": milestone_id,
                "bulk_stories": bulk_stories,
            },
        )
        return {
            "status": "updated",
            "project_id": project_id,
            "milestone_id": milestone_id,
            "stories_moved": len(bulk_stories),
        }

    return _execute_taiga_operation(
        "bulk_update_user_story_milestone",
        do_bulk,
        f"{len(bulk_stories)} stories to milestone {milestone_id}",
    )


@mcp.tool(
    "bulk_update_user_story_swimlane",
    description=(
        "Assigns multiple user stories to a kanban swimlane in a single call. "
        "Requires project_id, status_id, swimlane_id, and a list of user story IDs. "
        "Per-status constraint (Taiga API): all user stories in user_story_ids must "
        "currently share the given status_id — this endpoint is the same one Taiga's "
        "Kanban UI uses to drop a column-worth of cards into a swimlane. To move "
        "stories spanning multiple statuses, group by status and call this tool "
        "once per status group. The order of IDs in user_story_ids determines "
        "kanban_order within the (status, swimlane) cell. For single-story "
        'assignment, use update_user_story with kwargs={"swimlane": <id>} instead. '
        "Uses default session if session_id not provided."
    ),
)
def bulk_update_user_story_swimlane(
    project_id: int,
    status_id: int,
    swimlane_id: int,
    user_story_ids: List[int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk-assigns user stories sharing a status to a swimlane via Taiga's bulk_update_kanban_order endpoint."""
    if not user_story_ids:
        raise ValueError("user_story_ids list cannot be empty.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_update_user_story_swimlane: {len(user_story_ids)} stories -> "
        f"swimlane {swimlane_id} (status {status_id}) in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        taiga_client_wrapper.api.post(
            "/userstories/bulk_update_kanban_order",
            json={
                "project_id": project_id,
                "status_id": status_id,
                "swimlane_id": swimlane_id,
                "bulk_userstories": user_story_ids,
            },
        )
        return {
            "status": "updated",
            "project_id": project_id,
            "swimlane_id": swimlane_id,
            "status_id": status_id,
            "stories_moved": len(user_story_ids),
        }

    return _execute_taiga_operation(
        "bulk_update_user_story_swimlane",
        do_bulk,
        f"{len(user_story_ids)} stories to swimlane {swimlane_id} (status {status_id})",
    )


@mcp.tool(
    "bulk_update_user_story_order",
    description="Reorders multiple user stories in bulk. order_type must be 'backlog', 'kanban', or 'sprint'. bulk_stories is a list of {us_id, order} dicts. Uses default session if session_id not provided.",
)
def bulk_update_user_story_order(
    project_id: int,
    order_type: str,
    bulk_stories: List[Dict[str, int]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Reorders user stories in bulk for a given view (backlog, kanban, or sprint)."""
    valid_order_types = {"backlog", "kanban", "sprint"}
    order_type = order_type.strip().lower()
    if order_type not in valid_order_types:
        raise ValueError(
            f"Invalid order_type '{order_type}'. Must be one of: {sorted(valid_order_types)}"
        )
    if not bulk_stories:
        raise ValueError("bulk_stories list cannot be empty.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_update_user_story_order: {len(bulk_stories)} stories, "
        f"order_type={order_type} in project {project_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        endpoint = f"/userstories/bulk_update_{order_type}_order"
        taiga_client_wrapper.api.post(
            endpoint,
            json={"project_id": project_id, "bulk_stories": bulk_stories},
        )
        return {
            "status": "reordered",
            "project_id": project_id,
            "order_type": order_type,
            "stories_reordered": len(bulk_stories),
        }

    return _execute_taiga_operation(
        "bulk_update_user_story_order",
        do_bulk,
        f"{len(bulk_stories)} stories ({order_type}) in project {project_id}",
    )


@mcp.tool(
    "bulk_create_memberships",
    description="Invites multiple users to a project at once. Each invitation needs an email and role_id. Uses default session if session_id not provided.",
)
def bulk_create_memberships(
    project_id: int,
    members: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk-invites members to a project. members is a list of {role_id, email} dicts."""
    if not members:
        raise ValueError("Members list cannot be empty.")
    for m in members:
        if "email" not in m or "role_id" not in m:
            raise ValueError("Each member must have 'email' and 'role_id' fields.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_create_memberships: {len(members)} members in project {project_id}, "
        f"session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        result = taiga_client_wrapper.api.post(
            "/memberships/bulk_create",
            json={"project_id": project_id, "bulk_memberships": members},
        )
        return {
            "status": "invited",
            "project_id": project_id,
            "members_invited": result if isinstance(result, list) else len(members),
        }

    return _execute_taiga_operation(
        "bulk_create_memberships", do_bulk, f"{len(members)} members in project {project_id}"
    )


@mcp.tool(
    "bulk_link_user_stories_to_epic",
    description="Links multiple user stories to an epic in a single call. project_id is required to identify the project context. Uses default session if session_id not provided.",
)
def bulk_link_user_stories_to_epic(
    project_id: int,
    epic_id: int,
    user_story_ids: List[int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Links multiple user stories to an epic at once."""
    if not user_story_ids:
        raise ValueError("user_story_ids list cannot be empty.")
    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing bulk_link_user_stories_to_epic: {len(user_story_ids)} stories -> "
        f"epic {epic_id}, session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_bulk():
        taiga_client_wrapper.api.post(
            f"/epics/{epic_id}/related_userstories/bulk_create",
            json={"project_id": project_id, "bulk_userstories": user_story_ids},
        )
        return {
            "status": "linked",
            "epic_id": epic_id,
            "user_story_ids": user_story_ids,
            "count": len(user_story_ids),
        }

    return _execute_taiga_operation(
        "bulk_link_user_stories_to_epic",
        do_bulk,
        f"{len(user_story_ids)} stories to epic {epic_id}",
    )


# --- Search ---


@mcp.tool()
def search_project(
    project_id: int,
    text: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Search across a Taiga project for user stories, tasks, issues, wiki pages, and epics.

    Uses Taiga's built-in search endpoint to find items matching the given text query.

    Args:
        project_id: The project ID to search within
        text: The search query text
        session_id: Optional session ID (uses default if not provided)

    Returns:
        Dict with keys 'count' (total matches) and 'userstories', 'tasks', 'issues',
        'wikipages', 'epics' — each a list of matching items with core fields.
    """
    text = text.strip() if text else ""
    if not text:
        raise ValueError("Search text cannot be empty.")

    actual_session_id = _get_session_id(session_id)
    logger.info(
        f"Executing search_project in project {project_id} for session {actual_session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(actual_session_id)

    def do_search():
        result = taiga_client_wrapper.api.get(
            "/search", params={"project": project_id, "text": text}
        )
        return {
            "count": result.get("count", 0),
            "userstories": result.get("userstories", []),
            "tasks": result.get("tasks", []),
            "issues": result.get("issues", []),
            "wikipages": result.get("wikipages", []),
            "epics": result.get("epics", []),
        }

    return _execute_taiga_operation("search_project", do_search, f"project {project_id}")


VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _resolve_transport(argv: list[str] | None = None, env: dict[str, str] | None = None) -> str:
    """Determine the MCP transport from CLI flags or environment variable.

    Priority: CLI flags (--sse, --streamable-http) > TAIGA_TRANSPORT env var > default (stdio).

    Args:
        argv: Command-line arguments (defaults to sys.argv).
        env: Environment variables (defaults to os.environ).

    Returns:
        The transport name to use.
    """
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
            f"Unknown TAIGA_TRANSPORT value '{env_transport}', falling back to stdio. "
            f"Valid values: {', '.join(VALID_TRANSPORTS)}"
        )

    return "stdio"


# --- Run the server ---
def main() -> None:
    transport = _resolve_transport()
    logger.info(f"Starting Taiga Full server with {transport} transport")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
