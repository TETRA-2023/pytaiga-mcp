"""Workflow-oriented Taiga MCP server.

Exposes ~28 intent-based tools designed for everyday project management:
sprint planning, backlog grooming, team workload, epic tracking, task
breakdown, wiki, and comments. All tools accept human-readable names
(project slug, sprint name, status name, username) and resolve them to
API IDs internally.

Start with:
    TAIGA_SERVER_MODE=workflow  (default)
"""

import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pytaigaclient.exceptions import TaigaAPIError, TaigaException

from src.config import settings
from src.taiga_client import TaigaClientWrapper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logging.getLogger("pytaigaclient").setLevel(logging.WARNING)

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


# ---------------------------------------------------------------------------
# Session helpers (mirrors server_full pattern)
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
# Error handling (mirrors server_full pattern)
# ---------------------------------------------------------------------------

_TAIGA_API_ERROR_PLACEHOLDER = "No error message provided by API."


def _repair_taiga_api_error(e: TaigaAPIError) -> None:
    """Rewrite a TaigaAPIError message in-place when pytaigaclient dropped a DRF body."""
    if getattr(e, "error_detail", None) != _TAIGA_API_ERROR_PLACEHOLDER:
        return
    response = getattr(e, "response", None)
    if response is None:
        return
    try:
        body = response.json()
    except (ValueError, AttributeError, TypeError):
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
    context_str = f" for {error_context}" if error_context else ""
    try:
        return operation_callable()
    except TaigaAPIError as e:
        _repair_taiga_api_error(e)
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
# Name resolution helpers
# ---------------------------------------------------------------------------


def _cached(session_id: str, key: str, factory):
    """Get or lazily compute a session-scoped cache entry.

    Used by the resolvers below to avoid re-fetching project lookup tables
    (statuses, members, priorities, …) on every tool call within a session.
    The cache is cleared when the session is logged out.
    """
    cache = _session_cache_for(session_id)
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def _resolve_project(client: TaigaClientWrapper, project: Any) -> Dict[str, Any]:
    """Resolve a project slug (str) or ID (int) to the full project dict."""
    if isinstance(project, int) or (isinstance(project, str) and project.isdigit()):
        result = client.api.projects.get(int(project))
    else:
        result = client.api.get("/projects/by_slug", params={"slug": project})
    if not result:
        raise ValueError(f"Project '{project}' not found.")
    return result


def _resolve_sprint(client: TaigaClientWrapper, project_id: int, sprint: Any) -> Dict[str, Any]:
    """Resolve a sprint name (str), ID (int), or None (current) to a milestone dict.

    "Current" sprint: the single open milestone where
        estimated_start <= today <= estimated_finish.
    If none or multiple open milestones match, an explicit name is required.
    """
    milestones = client.list_resources("milestones", project_id=project_id)
    if sprint is None:
        today = date.today().isoformat()
        current = [
            m
            for m in milestones
            if not m.get("closed")
            and (m.get("estimated_start") or "9999-12-31")
            <= today
            <= (m.get("estimated_finish") or "0000-01-01")
        ]
        if len(current) == 1:
            return current[0]
        if len(current) == 0:
            raise ValueError(
                "No current sprint found (no open milestone covering today). "
                "Provide an explicit sprint name or ID."
            )
        names = [m["name"] for m in current]
        raise ValueError(f"Multiple active sprints found: {names}. Specify one explicitly.")
    if isinstance(sprint, int) or (isinstance(sprint, str) and sprint.isdigit()):
        matches = [m for m in milestones if m["id"] == int(sprint)]
    else:
        matches = [m for m in milestones if m["name"] == sprint]
    if not matches:
        available = [m["name"] for m in milestones]
        raise ValueError(f"Sprint '{sprint}' not found. Available: {available}")
    return matches[0]


_STATUS_RESOURCE_MAP = {
    "story": "userstory_statuses",
    "user_story": "userstory_statuses",
    "task": "task_statuses",
    "issue": "issue_statuses",
}

_ISSUE_ATTR_RESOURCE_MAP = {
    "priority": "priorities",
    "severity": "severities",
    "type": "issue_types",
}

# Allowed override keys for break_down_story per-task dicts. Kept aligned with
# the kwargs surface of create_task. Adding a field to create_task should
# update this set too.
_TASK_OVERRIDE_KEYS = {
    "subject",
    "description",
    "status",
    "assignee",
    "due_date",
    "tags",
    "blocked",
}


def _resolve_status(
    client: TaigaClientWrapper,
    project_id: int,
    entity_type: str,
    status_name: str,
    session_id: str,
) -> int:
    """Resolve a status name (case-insensitive) to its ID, cached per session."""
    resource = _STATUS_RESOURCE_MAP.get(entity_type)
    if not resource:
        raise ValueError(f"Cannot resolve statuses for entity type '{entity_type}'.")
    statuses = _cached(
        session_id,
        f"statuses_{entity_type}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    matches = [s for s in statuses if s["name"].lower() == status_name.lower()]
    if not matches:
        available = [s["name"] for s in statuses]
        raise ValueError(f"Status '{status_name}' not found. Available: {available}")
    return matches[0]["id"]


def _resolve_user(
    client: TaigaClientWrapper,
    project_id: int,
    username: str,
    session_id: str,
) -> int:
    """Resolve a username, email, or full name to a user ID within project members."""
    members = _cached(
        session_id,
        f"members_{project_id}",
        lambda: client.list_resources("memberships", project_id=project_id),
    )
    needle = username.lower()
    for m in members:
        info = m.get("user_extra_info") or {}
        if (
            info.get("username", "").lower() == needle
            or m.get("email", "").lower() == needle
            or m.get("full_name", "").lower() == needle
            or info.get("full_name_display", "").lower() == needle
        ):
            return m["user"]
    available = [m.get("full_name") or m.get("email", "?") for m in members]
    raise ValueError(f"User '{username}' not found in project. Members: {available}")


def _resolve_issue_defaults(
    client: TaigaClientWrapper, project_id: int, session_id: str
) -> Dict[str, Optional[int]]:
    """Return default priority, severity and type IDs for a project (cached).

    Each field is None when the project has no configured values for it; callers
    must guard against None before sending to the API.
    """

    def _fetch():
        priorities = client.list_resources("priorities", project_id=project_id)
        severities = client.list_resources("severities", project_id=project_id)
        types = client.list_resources("issue_types", project_id=project_id)
        return {
            "priority": priorities[0]["id"] if priorities else None,
            "severity": severities[0]["id"] if severities else None,
            "type": types[0]["id"] if types else None,
        }

    return _cached(session_id, f"issue_defaults_{project_id}", _fetch)


def _resolve_issue_attribute(
    client: TaigaClientWrapper,
    project_id: int,
    attr_type: str,
    name: str,
    session_id: str,
) -> int:
    """Resolve an issue priority, severity, or type name to its ID."""
    resource = _ISSUE_ATTR_RESOURCE_MAP.get(attr_type)
    if not resource:
        raise ValueError(f"Unknown attribute type '{attr_type}'.")
    items = _cached(
        session_id,
        f"{resource}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    matches = [i for i in items if i["name"].lower() == name.lower()]
    if not matches:
        available = [i["name"] for i in items]
        raise ValueError(f"{attr_type.capitalize()} '{name}' not found. Available: {available}")
    return matches[0]["id"]


def _resolve_story_refs(client: TaigaClientWrapper, project_id: int, refs: List[int]) -> List[int]:
    """Resolve a list of user story ref numbers to IDs with a single API call.

    Returns IDs in the same order as the input refs. Raises ValueError listing
    every missing ref so callers see the full diagnostic at once. Not cached:
    refs in batch-mutation tools often point at freshly-created stories that
    aren't in any prior cache snapshot.
    """
    if not refs:
        return []
    stories = client.list_resources("user_stories", project_id=project_id)
    by_ref = {s["ref"]: s["id"] for s in stories}
    missing = [r for r in refs if r not in by_ref]
    if missing:
        raise ValueError(f"User stories not found: {missing}")
    return [by_ref[r] for r in refs]


def _story_summary(us: Dict[str, Any]) -> Dict[str, Any]:
    """Extract readable fields from a raw user story dict."""
    return {
        "ref": us.get("ref"),
        "subject": us.get("subject"),
        "status": (us.get("status_extra_info") or {}).get("name") or us.get("status"),
        "assignee": (us.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "is_blocked": us.get("is_blocked", False),
        "is_closed": us.get("is_closed", False),
        "sprint": (us.get("milestone_extra_info") or {}).get("name"),
        "tags": us.get("tags", []),
    }


def _task_summary(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref": task.get("ref"),
        "subject": task.get("subject"),
        "status": (task.get("status_extra_info") or {}).get("name") or task.get("status"),
        "assignee": (task.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "is_blocked": task.get("is_blocked", False),
    }


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

_COMMENT_PATH_MAP = {
    "story": "userstories",
    "user_story": "userstories",
    "task": "tasks",
    "issue": "issues",
    "epic": "epics",
}

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
            wrapper = TaigaClientWrapper(host=settings.host)
            if wrapper.login(
                username=settings.get_username_value(),
                password=settings.get_password_value(),
            ):
                active_sessions[DEFAULT_SESSION_ID] = wrapper
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
        active_sessions.clear()
        _session_cache.clear()


_mcp_port_str = os.environ.get("MCP_PORT", "8000")
try:
    _mcp_port = int(_mcp_port_str)
except ValueError:
    logger.error(f"Invalid MCP_PORT '{_mcp_port_str}', falling back to 8000.")
    _mcp_port = 8000

mcp = FastMCP(
    "Taiga Workflow",
    dependencies=["pytaigaclient"],
    lifespan=server_lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=_mcp_port,
)

# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_default_session",
    description="Returns the default session ID if auto-authentication from environment variables was successful.",
)
def get_default_session() -> Dict[str, Any]:
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
        "message": "No default session. Set TAIGA_USERNAME/TAIGA_PASSWORD or call login().",
    }


@mcp.tool(
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
        wrapper = TaigaClientWrapper(host=actual_host)
        if wrapper.login(username=actual_username, password=actual_password):
            session_id = str(uuid.uuid4())
            active_sessions[session_id] = wrapper
            if DEFAULT_SESSION_ID not in active_sessions:
                active_sessions[DEFAULT_SESSION_ID] = wrapper
            return {"session_id": session_id}
        raise RuntimeError("Login failed.")
    except (ValueError, TaigaException) as e:
        logger.error(f"Login failed: {e}", exc_info=False)
        raise
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}", exc_info=True)
        raise RuntimeError("An unexpected server error occurred during login.")


@mcp.tool(
    "logout",
    description="Invalidate the current session. Uses the default session if session_id is not provided.",
)
def logout(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    active_sessions.pop(actual_session_id, None)
    _clear_session_cache(actual_session_id)
    return {"status": "logged_out", "session_id": actual_session_id}


@mcp.tool(
    "session_status",
    description="Check whether the current session is active and which user it belongs to.",
)
def session_status(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = active_sessions.get(actual_session_id)
    if client and client.is_authenticated:
        try:
            me = client.api.users.get_me()
            return {
                "status": "active",
                "session_id": actual_session_id,
                "username": me.get("username"),
            }
        except TaigaException:
            active_sessions.pop(actual_session_id, None)
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


@mcp.tool(
    "get_current_user",
    description=(
        "Return the identity of the authenticated user for this session. "
        "Useful when the agent needs to resolve 'me' — e.g. 'assign this to me' or 'show my tasks'. "
        "Returns username, display name, email, and user ID."
    ),
)
def get_current_user(session_id: Optional[str] = None) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_get_me():
        me = client.api.users.get_me()
        return {
            "id": me.get("id"),
            "username": me.get("username"),
            "full_name": me.get("full_name_display") or me.get("full_name"),
            "email": me.get("email"),
            "bio": me.get("bio") or None,
            "photo": me.get("photo") or None,
        }

    return _execute_taiga_operation("get_current_user", do_get_me)


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "list_projects",
    description="List all Taiga projects accessible to the authenticated user.",
)
def list_projects(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_list():
        projects = client.list_resources("projects")
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "slug": p["slug"],
                "description": p.get("description"),
                "is_private": p.get("is_private"),
            }
            for p in projects
        ]

    return _execute_taiga_operation("list_projects", do_list)


@mcp.tool(
    "get_project_overview",
    description=(
        "Return a full snapshot of a project: description, team, active sprint, "
        "and user story counts by status. project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_overview(
    project: Any,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_overview():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        today = date.today().isoformat()

        # Team
        members = client.list_resources("memberships", project_id=project_id)
        team = [
            {
                "username": (m.get("user_extra_info") or {}).get("username"),
                "full_name": m.get("full_name"),
                "role": m.get("role_name"),
                "is_admin": m.get("is_admin", False),
            }
            for m in members
        ]

        # Active sprint
        milestones = client.list_resources("milestones", project_id=project_id)
        current_sprints = [
            m
            for m in milestones
            if not m.get("closed")
            and (m.get("estimated_start") or "9999-12-31")
            <= today
            <= (m.get("estimated_finish") or "0000-01-01")
        ]
        active_sprint = None
        if len(current_sprints) == 1:
            m = current_sprints[0]
            active_sprint = {
                "name": m["name"],
                "start": m.get("estimated_start"),
                "end": m.get("estimated_finish"),
                "id": m["id"],
            }

        # Story counts by status
        stories = client.list_resources("user_stories", project_id=project_id)
        by_status: Dict[str, int] = {}
        for s in stories:
            status_name = (s.get("status_extra_info") or {}).get("name") or str(s.get("status"))
            by_status[status_name] = by_status.get(status_name, 0) + 1

        return {
            "project": {
                "id": proj["id"],
                "name": proj["name"],
                "slug": proj["slug"],
                "description": proj.get("description"),
                "is_private": proj.get("is_private"),
            },
            "team": team,
            "active_sprint": active_sprint,
            "stories_by_status": by_status,
            "total_stories": len(stories),
        }

    return _execute_taiga_operation("get_project_overview", do_overview, str(project))


@mcp.tool(
    "search",
    description=(
        "Full-text search within a project across user stories, tasks, issues, epics, and wiki pages. "
        "Returns grouped results. project accepts slug or ID."
    ),
)
def search(
    project: Any,
    query: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_search():
        proj = _resolve_project(client, project)
        result = client.api.get("/search", params={"project": proj["id"], "text": query})
        return {
            "query": query,
            "project": proj["slug"],
            "count": result.get("count", 0),
            "user_stories": result.get("userstories", []),
            "tasks": result.get("tasks", []),
            "issues": result.get("issues", []),
            "epics": result.get("epics", []),
            "wiki_pages": result.get("wikipages", []),
        }

    return _execute_taiga_operation("search", do_search, f"project={project} query={query!r}")


# ---------------------------------------------------------------------------
# Backlog tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "browse_backlog",
    description=(
        "List user stories in the backlog (not assigned to any sprint) with optional filters. "
        "Filters: status (name), assignee (username/email), epic (ref number), tags (comma-separated). "
        "project accepts slug or ID."
    ),
)
def browse_backlog(
    project: Any,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    epic: Optional[int] = None,
    tags: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_browse():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        filters: Dict[str, Any] = {"milestone": "null"}  # backlog = no sprint

        if status:
            status_id = _resolve_status(client, project_id, "story", status, actual_session_id)
            filters["status"] = status_id
        if assignee:
            user_id = _resolve_user(client, project_id, assignee, actual_session_id)
            filters["assigned_to"] = user_id
        if epic is not None:
            epic_data = client.api.get("/epics/by_ref", params={"ref": epic, "project": project_id})
            if not epic_data:
                raise ValueError(f"Epic #{epic} not found in project {proj['slug']}.")
            filters["epic"] = epic_data["id"]
        if tags:
            filters["tags"] = tags

        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        return [_story_summary(s) for s in stories]

    return _execute_taiga_operation("browse_backlog", do_browse, str(project))


@mcp.tool(
    "create_story",
    description=(
        "Create a user story in the backlog with optional epic link, sprint assignment, and assignee. "
        "project accepts slug or ID. assignee accepts username or email. "
        "sprint accepts name or ID (omit to place in backlog). epic accepts ref number."
    ),
)
def create_story(
    project: Any,
    subject: str,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Any] = None,
    epic: Optional[int] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_create():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        if description:
            payload["description"] = description
        if tags:
            payload["tags"] = tags
        if assignee:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)
        if sprint is not None:
            milestone = _resolve_sprint(client, project_id, sprint)
            payload["milestone"] = milestone["id"]

        result = client.api.user_stories.create(**payload)

        # Link to epic after creation
        if epic is not None and result:
            epic_data = client.api.get("/epics/by_ref", params={"ref": epic, "project": project_id})
            if not epic_data:
                raise ValueError(f"Epic #{epic} not found — story was created but not linked.")
            client.api.post(
                f"/epics/{epic_data['id']}/related_userstories",
                json={"project_id": project_id, "user_story_id": result["id"]},
            )

        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            "sprint": (result.get("milestone_extra_info") or {}).get("name"),
            "epic_linked": epic is not None,
        }

    return _execute_taiga_operation("create_story", do_create, str(project))


@mcp.tool(
    "update_story",
    description=(
        "Update a user story by its ref number. All fields are optional — only provided fields are changed. "
        "status, assignee, and sprint are resolved by name. "
        "epic accepts an epic ref number to link the story to that epic, or 0 to unlink from all epics. "
        "project accepts slug or ID."
    ),
)
def update_story(
    project: Any,
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Any] = None,
    epic: Optional[int] = None,
    tags: Optional[List[str]] = None,
    blocked: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_update():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        current = client.api.user_stories.get_by_ref(ref=ref, project=project_id)
        if not current:
            raise ValueError(f"User story #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": current["version"]}
        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = _resolve_status(
                client, project_id, "story", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)
        if sprint is not None:
            milestone = _resolve_sprint(client, project_id, sprint)
            payload["milestone"] = milestone["id"]
        if tags is not None:
            payload["tags"] = tags
        if blocked is not None:
            payload["is_blocked"] = blocked

        epic_changed = False
        # Only the body fields gate the "no-op" check; epic relinking is a
        # separate API call so a pure relink (epic=N, no other fields) is valid.
        if len(payload) == 1 and epic is None:
            raise ValueError("No fields to update were provided.")

        if epic is not None:
            # Validate the target epic BEFORE any mutation. If we deleted old
            # links first and the lookup then failed, the story would be left
            # orphaned despite the caller seeing an error.
            new_epic = None
            if epic != 0:
                new_epic = client.api.get(
                    "/epics/by_ref", params={"ref": epic, "project": project_id}
                )
                if not new_epic:
                    raise ValueError(f"Epic #{epic} not found in project '{proj['slug']}'.")

            # Taiga's UserStorySerializer returns `epics` as a list of dicts
            # ({"id": <epic_id>, "ref": ..., "subject": ..., "color": ...}),
            # not bare IDs — extract `id` from each entry.
            for old_epic in current.get("epics") or []:
                client.api.delete(f"/epics/{old_epic['id']}/related_userstories/{current['id']}")
            if new_epic is not None:
                client.api.post(
                    f"/epics/{new_epic['id']}/related_userstories",
                    json={"epic": new_epic["id"], "user_story": current["id"]},
                )
            epic_changed = True

        result = current
        if len(payload) > 1:
            result = client.api.user_stories.edit(current["id"], **payload)

        summary = _story_summary(result)
        if epic_changed:
            summary["epic"] = epic if epic else None
        return summary

    return _execute_taiga_operation("update_story", do_update, f"#{ref} in {project}")


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "create_task",
    description=(
        "Add a single task under an existing user story. story_ref is the parent US ref number. "
        "By default the task inherits the parent story's sprint; pass sprint=<name|id> to "
        "place the task in a different sprint, or sprint=0 to keep it out of any sprint. "
        "All name fields are resolved (assignee username, status name, sprint name). "
        "project accepts slug or ID."
    ),
)
def create_task(
    project: Any,
    story_ref: int,
    subject: str,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Any] = None,
    due_date: Optional[str] = None,
    tags: Optional[List[str]] = None,
    blocked: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_create():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        story = client.api.user_stories.get_by_ref(ref=story_ref, project=project_id)
        if not story:
            raise ValueError(f"User story #{story_ref} not found in project '{proj['slug']}'.")

        data: Dict[str, Any] = {"user_story": story["id"]}

        # Milestone handling: Taiga does NOT auto-copy the parent story's
        # milestone onto a new task — Task and UserStory are independent FKs.
        # We replicate the sprint-board UI behaviour by defaulting to the
        # parent's milestone. Callers can override with sprint=<name|id> or
        # explicitly opt out with sprint=0 (or "0" — both accepted).
        # `str(sprint) == "0"` catches int 0 and string "0" without matching
        # bool False (str(False) == "False"), which keeps the opt-out path
        # honest about what counts as "no sprint".
        if sprint is None:
            if story.get("milestone") is not None:
                data["milestone"] = story["milestone"]
        elif str(sprint) == "0":
            pass  # explicit "no sprint"
        else:
            data["milestone"] = _resolve_sprint(client, project_id, sprint)["id"]

        if description:
            data["description"] = description
        if tags:
            data["tags"] = tags
        if due_date:
            data["due_date"] = due_date
        if blocked is not None:
            data["is_blocked"] = blocked
        if status:
            data["status"] = _resolve_status(client, project_id, "task", status, actual_session_id)
        if assignee:
            data["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)

        result = client.api.tasks.create(project=project_id, subject=subject, data=data)
        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            "parent_story_ref": story_ref,
        }

    return _execute_taiga_operation("create_task", do_create, f"under US #{story_ref} in {project}")


@mcp.tool(
    "update_task",
    description=(
        "Update a task by its ref number. All fields are optional — only provided fields change. "
        "status, assignee, and sprint are resolved by name. story_ref reparents the task to a "
        "different user story. sprint accepts a name or ID; tasks can have a milestone "
        "independent of their parent story. project accepts slug or ID."
    ),
)
def update_task(
    project: Any,
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Any] = None,
    story_ref: Optional[int] = None,
    blocked: Optional[bool] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_update():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        # Workaround: pytaigaclient tasks.get_by_ref passes query_params= but
        # TaigaClient.get() expects params=. Bypass via direct API call.
        current = client.api.get("/tasks/by_ref", params={"ref": ref, "project": project_id})
        if not current:
            raise ValueError(f"Task #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": current["version"]}
        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = _resolve_status(
                client, project_id, "task", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)
        if sprint is not None:
            milestone = _resolve_sprint(client, project_id, sprint)
            payload["milestone"] = milestone["id"]
        if story_ref is not None:
            new_story = client.api.user_stories.get_by_ref(ref=story_ref, project=project_id)
            if not new_story:
                raise ValueError(f"User story #{story_ref} not found in project '{proj['slug']}'.")
            payload["user_story"] = new_story["id"]
        if blocked is not None:
            payload["is_blocked"] = blocked
        if tags is not None:
            payload["tags"] = tags

        if len(payload) == 1:  # only version key
            raise ValueError("No fields to update were provided.")

        result = client.api.tasks.edit(current["id"], **payload)
        return _task_summary(result)

    return _execute_taiga_operation("update_task", do_update, f"task #{ref} in {project}")


@mcp.tool(
    "break_down_story",
    description=(
        "Decompose a user story into multiple tasks in one call. tasks accepts a list of "
        "subject strings (e.g. ['design', 'API', 'tests']) or a list of dicts "
        "({'subject': str, 'assignee': str?, 'status': str?, 'description': str?, "
        "'due_date': str?, 'tags': [str]?, 'blocked': bool?}) for per-task overrides. "
        "Tasks inherit the parent story's sprint. Performance: when the story is in a "
        "sprint AND no per-task overrides are provided, the bulk endpoint creates all "
        "tasks in one API call; otherwise (story in backlog OR overrides present) tasks "
        "are created individually. Unknown override keys raise ValueError. "
        "project accepts slug or ID."
    ),
)
def break_down_story(
    project: Any,
    story_ref: int,
    tasks: List[Any],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not tasks:
        raise ValueError("tasks list cannot be empty.")
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_break_down():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        story = client.api.user_stories.get_by_ref(ref=story_ref, project=project_id)
        if not story:
            raise ValueError(f"User story #{story_ref} not found in project '{proj['slug']}'.")

        # Normalize entries into dicts and validate override keys.
        normalized: List[Dict[str, Any]] = []
        for entry in tasks:
            if isinstance(entry, str):
                normalized.append({"subject": entry})
            elif isinstance(entry, dict) and entry.get("subject"):
                unknown = set(entry.keys()) - _TASK_OVERRIDE_KEYS
                if unknown:
                    raise ValueError(
                        f"Unknown per-task override keys: {sorted(unknown)}. "
                        f"Allowed: {sorted(_TASK_OVERRIDE_KEYS)}."
                    )
                normalized.append(entry)
            else:
                raise ValueError(
                    "Each entry in tasks must be a non-empty string or a dict with a 'subject' key."
                )

        # Bulk path requires sprint context AND no per-task overrides — Taiga's
        # /tasks/bulk_create only accepts a flat subject list scoped to one milestone.
        has_overrides = any(set(e.keys()) - {"subject"} for e in normalized)
        milestone_id = story.get("milestone")

        if not has_overrides and milestone_id is not None:
            bulk_tasks = "\n".join(e["subject"] for e in normalized)
            result = client.api.post(
                "/tasks/bulk_create",
                json={
                    "project_id": project_id,
                    "bulk_tasks": bulk_tasks,
                    "milestone_id": milestone_id,
                    "us_id": story["id"],
                },
            )
            if isinstance(result, list):
                created = result
            else:
                logger.warning(
                    f"Unexpected /tasks/bulk_create response shape "
                    f"({type(result).__name__}); reporting 0 created."
                )
                created = []
        else:
            # Loop path: story in backlog (no milestone) OR per-task overrides present.
            # Tasks inherit the parent story's milestone when available.
            created = []
            for entry in normalized:
                data: Dict[str, Any] = {"user_story": story["id"]}
                if milestone_id is not None:
                    data["milestone"] = milestone_id
                if "description" in entry:
                    data["description"] = entry["description"]
                if "due_date" in entry:
                    data["due_date"] = entry["due_date"]
                if "tags" in entry:
                    data["tags"] = entry["tags"]
                if "blocked" in entry:
                    data["is_blocked"] = entry["blocked"]
                if "status" in entry:
                    data["status"] = _resolve_status(
                        client, project_id, "task", entry["status"], actual_session_id
                    )
                if "assignee" in entry:
                    data["assigned_to"] = _resolve_user(
                        client, project_id, entry["assignee"], actual_session_id
                    )
                task = client.api.tasks.create(
                    project=project_id, subject=entry["subject"], data=data
                )
                created.append(task)

        return {
            "status": "decomposed",
            "story_ref": story_ref,
            "tasks_created": len(created),
            "tasks": [{"ref": t.get("ref"), "subject": t.get("subject")} for t in created],
        }

    return _execute_taiga_operation(
        "break_down_story", do_break_down, f"US #{story_ref} in {project}"
    )


@mcp.tool(
    "create_issue",
    description=(
        "Create a new issue. type, priority, and severity default to the project's first configured value "
        "when not provided. All name fields are resolved (project slug, assignee username). "
        "project accepts slug or ID."
    ),
)
def create_issue(
    project: Any,
    subject: str,
    description: Optional[str] = None,
    issue_type: Optional[str] = None,
    priority: Optional[str] = None,
    severity: Optional[str] = None,
    assignee: Optional[str] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_create():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        defaults = _resolve_issue_defaults(client, project_id, actual_session_id)

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        # Apply project defaults only when the project actually has them configured.
        # Sending None would produce a 400 from Taiga.
        for field in ("priority", "severity", "type"):
            if defaults.get(field) is not None:
                payload[field] = defaults[field]

        if description:
            payload["description"] = description
        if tags:
            payload["tags"] = tags
        if issue_type:
            payload["type"] = _resolve_issue_attribute(
                client, project_id, "type", issue_type, actual_session_id
            )
        if priority:
            payload["priority"] = _resolve_issue_attribute(
                client, project_id, "priority", priority, actual_session_id
            )
        if severity:
            payload["severity"] = _resolve_issue_attribute(
                client, project_id, "severity", severity, actual_session_id
            )
        if assignee:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)

        # Default status = first configured issue status, when available.
        statuses = _cached(
            actual_session_id,
            f"statuses_issue_{project_id}",
            lambda: client.list_resources("issue_statuses", project_id=project_id),
        )
        if statuses:
            payload["status"] = statuses[0]["id"]

        data = {k: v for k, v in payload.items() if k not in ("project", "subject")}
        result = client.api.issues.create(
            project=project_id,
            subject=subject,
            data=data if data else None,
        )
        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            "type": (result.get("type_extra_info") or {}).get("name"),
            "priority": (result.get("priority_extra_info") or {}).get("name"),
            "severity": (result.get("severity_extra_info") or {}).get("name"),
        }

    return _execute_taiga_operation("create_issue", do_create, str(project))


@mcp.tool(
    "update_issue",
    description=(
        "Update an issue by ref number. All fields optional. "
        "status, priority, severity, type, and assignee are resolved by name. "
        "project accepts slug or ID."
    ),
)
def update_issue(
    project: Any,
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    priority: Optional[str] = None,
    severity: Optional[str] = None,
    issue_type: Optional[str] = None,
    blocked: Optional[bool] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_update():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        current = client.api.issues.get_by_ref(ref=ref, project=project_id)
        if not current:
            raise ValueError(f"Issue #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": current["version"]}
        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = _resolve_status(
                client, project_id, "issue", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)
        if priority is not None:
            payload["priority"] = _resolve_issue_attribute(
                client, project_id, "priority", priority, actual_session_id
            )
        if severity is not None:
            payload["severity"] = _resolve_issue_attribute(
                client, project_id, "severity", severity, actual_session_id
            )
        if issue_type is not None:
            payload["type"] = _resolve_issue_attribute(
                client, project_id, "type", issue_type, actual_session_id
            )
        if blocked is not None:
            payload["is_blocked"] = blocked
        if tags is not None:
            payload["tags"] = tags

        if len(payload) == 1:
            raise ValueError("No fields to update were provided.")

        result = client.api.issues.edit(current["id"], **payload)
        return {
            "ref": result.get("ref"),
            "subject": result.get("subject"),
            "status": (result.get("status_extra_info") or {}).get("name"),
            "priority": (result.get("priority_extra_info") or {}).get("name"),
            "severity": (result.get("severity_extra_info") or {}).get("name"),
            "type": (result.get("type_extra_info") or {}).get("name"),
            "assignee": (result.get("assigned_to_extra_info") or {}).get("full_name_display"),
            "is_blocked": result.get("is_blocked"),
        }

    return _execute_taiga_operation("update_issue", do_update, f"#{ref} in {project}")


# ---------------------------------------------------------------------------
# Sprint tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_sprint_board",
    description=(
        "Return a full sprint board: sprint metadata, all user stories with their tasks, "
        "and a status summary. sprint accepts a name or ID; omit to use the current sprint "
        "(single open milestone covering today). with_tasks=True (default) fetches tasks per story. "
        "project accepts slug or ID."
    ),
)
def get_sprint_board(
    project: Any,
    sprint: Optional[Any] = None,
    with_tasks: bool = True,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_board():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        milestone = _resolve_sprint(client, project_id, sprint)
        milestone_id = milestone["id"]

        stories = client.list_resources(
            "user_stories", project_id=project_id, milestone=milestone_id
        )

        # Fetch all sprint tasks in one call, group by user_story id
        tasks_by_story: Dict[int, List[Dict]] = {}
        if with_tasks and stories:
            all_tasks = client.list_resources(
                "tasks", project_id=project_id, milestone=milestone_id
            )
            for t in all_tasks:
                us_id = t.get("user_story")
                if us_id is not None:
                    tasks_by_story.setdefault(us_id, []).append(t)

        # Build story rows
        story_rows = []
        by_status: Dict[str, int] = {}
        blocked_count = 0
        for s in stories:
            status_name = (s.get("status_extra_info") or {}).get("name") or str(s.get("status"))
            by_status[status_name] = by_status.get(status_name, 0) + 1
            if s.get("is_blocked"):
                blocked_count += 1
            row = _story_summary(s)
            if with_tasks:
                row["tasks"] = [_task_summary(t) for t in tasks_by_story.get(s["id"], [])]
            story_rows.append(row)

        total_tasks = sum(len(v) for v in tasks_by_story.values()) if with_tasks else None

        return {
            "sprint": {
                "id": milestone["id"],
                "name": milestone["name"],
                "start": milestone.get("estimated_start"),
                "end": milestone.get("estimated_finish"),
                "is_closed": milestone.get("closed", False),
            },
            "summary": {
                "total_stories": len(stories),
                "stories_by_status": by_status,
                "blocked": blocked_count,
                **({"total_tasks": total_tasks} if with_tasks else {}),
            },
            "stories": story_rows,
        }

    return _execute_taiga_operation("get_sprint_board", do_board, str(project))


@mcp.tool(
    "plan_sprint",
    description=(
        "Create a new sprint and optionally assign user stories to it in one step. "
        "story_refs is a list of user story ref numbers (e.g. [12, 15, 23]). "
        "Dates must be ISO 8601 (YYYY-MM-DD). project accepts slug or ID."
    ),
)
def plan_sprint(
    project: Any,
    name: str,
    start_date: str,
    end_date: str,
    story_refs: Optional[List[int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_plan():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        milestone = client.api.milestones.create(
            project=project_id,
            name=name,
            estimated_start=start_date,
            estimated_finish=end_date,
        )
        milestone_id = milestone["id"]

        stories_moved = 0
        if story_refs:
            us_ids = _resolve_story_refs(client, project_id, story_refs)
            bulk_stories = [{"us_id": uid, "order": i} for i, uid in enumerate(us_ids)]
            client.api.post(
                "/userstories/bulk_update_milestone",
                json={
                    "project_id": project_id,
                    "milestone_id": milestone_id,
                    "bulk_stories": bulk_stories,
                },
            )
            stories_moved = len(bulk_stories)

        return {
            "status": "created",
            "sprint": {
                "id": milestone_id,
                "name": milestone["name"],
                "start": milestone.get("estimated_start"),
                "end": milestone.get("estimated_finish"),
            },
            "stories_assigned": stories_moved,
        }

    return _execute_taiga_operation("plan_sprint", do_plan, str(project))


@mcp.tool(
    "move_to_sprint",
    description=(
        "Move one or more user stories (by ref number) into a sprint. "
        "sprint accepts a name or ID. story_refs is a list of ref numbers. "
        "project accepts slug or ID."
    ),
)
def move_to_sprint(
    project: Any,
    story_refs: List[int],
    sprint: Any,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_move():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        milestone = _resolve_sprint(client, project_id, sprint)

        us_ids = _resolve_story_refs(client, project_id, story_refs)
        bulk_stories = [{"us_id": uid, "order": i} for i, uid in enumerate(us_ids)]

        client.api.post(
            "/userstories/bulk_update_milestone",
            json={
                "project_id": project_id,
                "milestone_id": milestone["id"],
                "bulk_stories": bulk_stories,
            },
        )
        return {
            "status": "moved",
            "sprint": milestone["name"],
            "stories_moved": len(bulk_stories),
            "refs": story_refs,
        }

    return _execute_taiga_operation("move_to_sprint", do_move, str(project))


@mcp.tool(
    "set_story_status",
    description=(
        "Change the status of a user story by its ref number using the status name "
        "(e.g. 'In Progress', 'Done'). project accepts slug or ID."
    ),
)
def set_story_status(
    project: Any,
    ref: int,
    status: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_set():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        status_id = _resolve_status(client, project_id, "story", status, actual_session_id)

        current = client.api.user_stories.get_by_ref(ref=ref, project=project_id)
        if not current:
            raise ValueError(f"User story #{ref} not found in project '{proj['slug']}'.")

        result = client.api.user_stories.edit(
            current["id"], status=status_id, version=current["version"]
        )
        return {
            "ref": ref,
            "status": (result.get("status_extra_info") or {}).get("name") or status,
            "is_closed": result.get("is_closed", False),
        }

    return _execute_taiga_operation("set_story_status", do_set, f"#{ref} in {project}")


@mcp.tool(
    "set_task_status",
    description=(
        "Change the status of a task by its ref number using the status name "
        "(e.g. 'In Progress', 'Done'). project accepts slug or ID."
    ),
)
def set_task_status(
    project: Any,
    ref: int,
    status: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_set():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        status_id = _resolve_status(client, project_id, "task", status, actual_session_id)

        # Workaround: pytaigaclient tasks.get_by_ref passes query_params= but
        # TaigaClient.get() expects params=. Bypass via direct API call.
        current = client.api.get("/tasks/by_ref", params={"ref": ref, "project": project_id})
        if not current:
            raise ValueError(f"Task #{ref} not found in project '{proj['slug']}'.")

        result = client.api.tasks.edit(current["id"], status=status_id, version=current["version"])
        return {
            "ref": ref,
            "status": (result.get("status_extra_info") or {}).get("name") or status,
            "is_closed": result.get("is_closed", False),
        }

    return _execute_taiga_operation("set_task_status", do_set, f"task #{ref} in {project}")


@mcp.tool(
    "close_sprint",
    description=(
        "Mark a sprint as closed. sprint accepts a name or ID; "
        "omit to close the current sprint (single open milestone covering today). "
        "project accepts slug or ID."
    ),
)
def close_sprint(
    project: Any,
    sprint: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_close():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        milestone = _resolve_sprint(client, project_id, sprint)

        result = client.api.milestones.edit(
            milestone["id"], closed=True, version=milestone.get("version", 1)
        )
        return {
            "status": "closed",
            "sprint": result.get("name"),
            "id": result.get("id"),
        }

    return _execute_taiga_operation("close_sprint", do_close, str(project))


# ---------------------------------------------------------------------------
# Epic tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_epic_overview",
    description=(
        "Return an epic and all user stories linked to it, with per-status counts. "
        "ref is the epic ref number. project accepts slug or ID."
    ),
)
def get_epic_overview(
    project: Any,
    ref: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_overview():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        epic = client.api.get("/epics/by_ref", params={"ref": ref, "project": project_id})
        if not epic:
            raise ValueError(f"Epic #{ref} not found in project '{proj['slug']}'.")

        # Single batched call instead of one /user_stories/<id> per linked story.
        linked_stories = client.list_resources(
            "user_stories", project_id=project_id, epic=epic["id"]
        )

        stories = [_story_summary(s) for s in linked_stories]
        by_status: Dict[str, int] = {}
        for s in stories:
            sname = s.get("status") or "Unknown"
            by_status[sname] = by_status.get(sname, 0) + 1

        return {
            "epic": {
                "ref": epic.get("ref"),
                "subject": epic.get("subject"),
                "status": (epic.get("status_extra_info") or {}).get("name"),
                "assignee": (epic.get("assigned_to_extra_info") or {}).get("full_name_display"),
                "color": epic.get("color"),
            },
            "summary": {
                "total_stories": len(stories),
                "stories_by_status": by_status,
            },
            "stories": stories,
        }

    return _execute_taiga_operation("get_epic_overview", do_overview, f"#{ref} in {project}")


@mcp.tool(
    "create_epic",
    description=(
        "Create a new epic and optionally link existing user stories to it by ref number. "
        "assignee accepts username or email. story_refs is a list of existing US ref numbers to link. "
        "project accepts slug or ID."
    ),
)
def create_epic(
    project: Any,
    subject: str,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    color: Optional[str] = None,
    story_refs: Optional[List[int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_create():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        if description:
            payload["description"] = description
        if color:
            payload["color"] = color
        if assignee:
            payload["assigned_to"] = _resolve_user(client, project_id, assignee, actual_session_id)

        epic = client.api.epics.create(**payload)
        epic_id = epic["id"]

        linked = 0
        if story_refs:
            us_ids = _resolve_story_refs(client, project_id, story_refs)
            client.api.post(
                f"/epics/{epic_id}/related_userstories/bulk_create",
                json={"project_id": project_id, "bulk_userstories": us_ids},
            )
            linked = len(us_ids)

        return {
            "status": "created",
            "ref": epic.get("ref"),
            "id": epic_id,
            "subject": epic.get("subject"),
            "stories_linked": linked,
        }

    return _execute_taiga_operation("create_epic", do_create, str(project))


# ---------------------------------------------------------------------------
# Team tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_team_workload",
    description=(
        "Show per-member story and task counts for a sprint or the full project backlog. "
        "sprint accepts name or ID; omit for whole-project view. project accepts slug or ID."
    ),
)
def get_team_workload(
    project: Any,
    sprint: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_workload():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        filters: Dict[str, Any] = {}
        sprint_name = None
        if sprint is not None:
            milestone = _resolve_sprint(client, project_id, sprint)
            filters["milestone"] = milestone["id"]
            sprint_name = milestone["name"]

        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        tasks = client.list_resources("tasks", project_id=project_id, **filters)

        # Aggregate per assignee
        workload: Dict[str, Dict[str, Any]] = {}

        def _add(name: Optional[str], category: str):
            if not name:
                name = "(unassigned)"
            if name not in workload:
                workload[name] = {"stories": 0, "tasks": 0, "blocked_stories": 0}
            workload[name][category] += 1

        for s in stories:
            name = (s.get("assigned_to_extra_info") or {}).get("full_name_display")
            _add(name, "stories")
            if s.get("is_blocked"):
                _add(name, "blocked_stories")

        for t in tasks:
            name = (t.get("assigned_to_extra_info") or {}).get("full_name_display")
            _add(name, "tasks")

        return {
            "project": proj["slug"],
            "sprint": sprint_name,
            "team": [{"member": name, **counts} for name, counts in sorted(workload.items())],
        }

    return _execute_taiga_operation("get_team_workload", do_workload, str(project))


@mcp.tool(
    "assign_item",
    description=(
        "Assign a user story, task, issue, or epic to a team member by username or email. "
        "entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "ref is the item ref number. project accepts slug or ID."
    ),
)
def assign_item(
    project: Any,
    ref: int,
    username: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    type_map = {
        "story": ("user_stories", "user_story"),
        "user_story": ("user_stories", "user_story"),
        "task": ("tasks", "task"),
        "issue": ("issues", "issue"),
        "epic": ("epics", "epic"),
    }
    if entity_type not in type_map:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(type_map)}."
        )

    def do_assign():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        user_id = _resolve_user(client, project_id, username, actual_session_id)

        collection_name, _ = type_map[entity_type]
        collection = getattr(client.api, collection_name)
        current = collection.get_by_ref(ref=ref, project=project_id)
        if not current:
            raise ValueError(f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'.")

        result = collection.edit(current["id"], assigned_to=user_id, version=current["version"])
        return {
            "ref": ref,
            "entity_type": entity_type,
            "assigned_to": (result.get("assigned_to_extra_info") or {}).get("full_name_display")
            or username,
        }

    return _execute_taiga_operation("assign_item", do_assign, f"{entity_type} #{ref} in {project}")


# ---------------------------------------------------------------------------
# Wiki tools
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_wiki",
    description=(
        "Get a wiki page by slug, or list all wiki pages when slug is omitted. "
        "project accepts slug or ID."
    ),
)
def get_wiki(
    project: Any,
    slug: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Any:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_get():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        if slug:
            page = client.api.get("/wiki/by_slug", params={"slug": slug, "project": project_id})
            if not page:
                raise ValueError(f"Wiki page '{slug}' not found in project '{proj['slug']}'.")
            return {"slug": page.get("slug"), "content": page.get("content"), "id": page.get("id")}

        pages = client.list_resources("wiki", project_id=project_id)
        return [{"slug": p.get("slug"), "id": p.get("id")} for p in pages]

    return _execute_taiga_operation("get_wiki", do_get, str(project))


@mcp.tool(
    "upsert_wiki",
    description=(
        "Create or update a wiki page. If a page with the given slug already exists it is updated; "
        "otherwise a new page is created. project accepts slug or ID."
    ),
)
def upsert_wiki(
    project: Any,
    slug: str,
    content: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_upsert():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        existing = client.api.get("/wiki/by_slug", params={"slug": slug, "project": project_id})
        if existing:
            result = client.api.wiki.edit(
                existing["id"], slug=slug, content=content, version=existing["version"]
            )
            return {"status": "updated", "slug": result.get("slug"), "id": result.get("id")}
        else:
            result = client.api.wiki.create(project=project_id, slug=slug, content=content)
            return {"status": "created", "slug": result.get("slug"), "id": result.get("id")}

    return _execute_taiga_operation("upsert_wiki", do_upsert, str(project))


# ---------------------------------------------------------------------------
# Project health tool
# ---------------------------------------------------------------------------


@mcp.tool(
    "get_project_health",
    description=(
        "Return a metrics snapshot for a project: story point completion, computed velocity speed "
        "(points/day), sprint-by-sprint velocity history, open issue counts by priority, and which "
        "modules are active. "
        "Complements get_project_overview (structure) with quantitative health signals. "
        "project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_health(
    project: Any,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    def do_health():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        stats = client.api.get(f"/projects/{project_id}/stats") or {}
        issues_stats = client.api.get(f"/projects/{project_id}/issues_stats") or {}
        modules = client.api.get(f"/projects/{project_id}/modules") or {}

        # Sprint velocity: list of closed_points per milestone (oldest → newest)
        milestones = stats.get("milestones") or []
        velocity = [
            {"sprint": m.get("name"), "closed_points": m.get("closed_points", 0)}
            for m in milestones
        ]

        # Issues breakdown by priority
        issues_by_priority = {
            entry.get("name", "unknown"): entry.get("count", 0)
            for entry in (issues_stats.get("issues_per_priority") or [])
        }

        # Active modules: slice off "is_" prefix (3 chars) and "_activated" suffix (10 chars)
        active_modules = [
            name[3:-10]
            for name, enabled in modules.items()
            if name.startswith("is_") and name.endswith("_activated") and enabled
        ]

        return {
            "project": {"id": proj["id"], "name": proj["name"], "slug": proj["slug"]},
            "stories": {
                "total_points": stats.get("total_points", 0),
                "closed_points": stats.get("closed_points", 0),
                "assigned_points": stats.get("assigned_points", 0),
                "total_milestones": stats.get("total_milestones", 0),
                "speed": stats.get("speed", 0),
            },
            "issues": {
                "total": issues_stats.get("total_issues", 0),
                "open": issues_stats.get("opened_issues", 0),
                "closed": issues_stats.get("closed_issues", 0),
                "by_priority": issues_by_priority,
            },
            "velocity": velocity,
            "active_modules": active_modules,
        }

    return _execute_taiga_operation("get_project_health", do_health, str(project))


# ---------------------------------------------------------------------------
# Project activity tool
# ---------------------------------------------------------------------------

# Map Taiga timeline event_type prefixes to readable entity labels.
_TIMELINE_ENTITY_MAP = {
    "userstories": "user_story",
    "tasks": "task",
    "issues": "issue",
    "epics": "epic",
    "milestones": "sprint",
    "wiki": "wiki_page",
    "projects": "project",
}


def _summarize_timeline_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Distil a raw Taiga timeline event into a readable summary dict."""
    event_type = event.get("event_type", "")
    parts = event_type.split(".")
    entity = _TIMELINE_ENTITY_MAP.get(parts[0], parts[0]) if parts[0] else "unknown"
    action = parts[-1] if len(parts) >= 2 else "unknown"

    data = event.get("data") or {}
    user_info = data.get("user") or {}
    actor = user_info.get("name") or user_info.get("username") or "unknown"

    values_diff = data.get("values_diff") or {}
    changed_fields = list(values_diff.keys()) if values_diff else []

    comment = data.get("comment") or ""

    summary: Dict[str, Any] = {
        "when": event.get("created"),
        "actor": actor,
        "entity": entity,
        "action": action,
        "object_id": event.get("object_id"),
    }
    if changed_fields:
        summary["changed_fields"] = changed_fields
    if comment:
        summary["comment"] = comment[:200]  # truncate long comments
    return summary


@mcp.tool(
    "get_project_activity",
    description=(
        "Return recent activity on a project — who did what to which entity and when. "
        "Answers 'what changed today/this week?' for a PO-level audit. "
        "limit controls how many events to return (default 20, max 100). "
        "project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_activity(
    project: Any,
    limit: int = 20,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100.")

    def do_activity():
        proj = _resolve_project(client, project)
        project_id = proj["id"]

        raw = client.api.get(
            f"/timeline/project/{project_id}",
            params={"page_size": limit},
        )
        events = raw if isinstance(raw, list) else []
        return {
            "project": {"id": proj["id"], "name": proj["name"], "slug": proj["slug"]},
            "count": len(events),
            "events": [_summarize_timeline_event(e) for e in events],
        }

    return _execute_taiga_operation("get_project_activity", do_activity, str(project))


# ---------------------------------------------------------------------------
# Comment tool
# ---------------------------------------------------------------------------


@mcp.tool(
    "add_comment",
    description=(
        "Add a comment to a user story, task, issue, or epic identified by ref number. "
        "entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "project accepts slug or ID."
    ),
)
def add_comment(
    project: Any,
    ref: int,
    text: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = _get_session_id(session_id)
    client = _get_authenticated_client(actual_session_id)

    if entity_type not in _COMMENT_PATH_MAP:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_COMMENT_PATH_MAP)}."
        )
    if not text or not text.strip():
        raise ValueError("Comment text must not be empty.")
    text = text.replace("\\n", "\n").replace("\\t", "\t")

    def do_comment():
        proj = _resolve_project(client, project)
        project_id = proj["id"]
        path_segment = _COMMENT_PATH_MAP[entity_type]

        # Resolve ref → ID
        collection_name = {
            "story": "user_stories",
            "user_story": "user_stories",
            "task": "tasks",
            "issue": "issues",
            "epic": "epics",
        }[entity_type]
        collection = getattr(client.api, collection_name)
        current = collection.get_by_ref(ref=ref, project=project_id)
        if not current:
            raise ValueError(f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'.")

        client.api.patch(
            f"/{path_segment}/{current['id']}",
            json={"comment": text, "version": current["version"]},
        )
        return {"status": "comment_added", "entity_type": entity_type, "ref": ref}

    return _execute_taiga_operation("add_comment", do_comment, f"{entity_type} #{ref} in {project}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    transport = _resolve_transport()
    logger.info(f"Starting Taiga Workflow server with {transport} transport")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
