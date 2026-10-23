"""Name resolution (project, sprint, status, user, refs) and result summaries."""

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

from src.server import core
from src.taiga_client import (
    TaigaClientWrapper,
    TaigaConcurrencyError,
    TaigaNotFoundError,
    resolve_user_id,
    safe_lower,
)

logger = logging.getLogger(__name__)


def _resolve_project(client: TaigaClientWrapper, project: Any) -> Dict[str, Any]:
    """Resolve a project slug (str) or ID (int) to the full project dict."""
    # Real Taiga answers an unknown slug or ID with a 404, never an empty body.
    try:
        if isinstance(project, int) or (isinstance(project, str) and project.isdigit()):
            result = client.api.get(f"/projects/{int(project)}")
        else:
            result = client.api.get("/projects/by_slug", params={"slug": project})
    except TaigaNotFoundError:
        result = None
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
    if isinstance(sprint, bool):
        raise ValueError(f"Sprint must be a name or an ID, got {sprint!r}.")
    # An int is an ID. A string is a name first: a sprint named "42" must not
    # resolve to the (different) sprint whose id is 42 — delete_item acts on the
    # result. A digit string that names no sprint falls back to an ID.
    if isinstance(sprint, int):
        matches = [m for m in milestones if m["id"] == sprint]
    else:
        matches = [m for m in milestones if m["name"] == sprint]
        if not matches and isinstance(sprint, str) and sprint.isdigit():
            matches = [m for m in milestones if m["id"] == int(sprint)]
    if not matches:
        available = [m["name"] for m in milestones]
        raise ValueError(f"Sprint '{sprint}' not found. Available: {available}")
    return matches[0]


# entity_type -> REST path segment (also the by_ref route prefix).
# Shared by add_comment and assign_item.
_ENTITY_PATH_MAP = {
    "story": "userstories",
    "user_story": "userstories",
    "task": "tasks",
    "issue": "issues",
    "epic": "epics",
}

_STATUS_RESOURCE_MAP = {
    "story": "userstory_statuses",
    "user_story": "userstory_statuses",
    "task": "task_statuses",
    "issue": "issue_statuses",
    "epic": "epic_statuses",
}

_ISSUE_ATTR_RESOURCE_MAP = {
    "priority": "priorities",
    "severity": "severities",
    "type": "issue_types",
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
    statuses = core._cached(
        session_id,
        f"statuses_{entity_type}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    matches = [s for s in statuses if safe_lower(s.get("name")) == safe_lower(status_name)]
    if not matches:
        available = [s["name"] for s in statuses]
        raise ValueError(f"Status '{status_name}' not found. Available: {available}")
    return matches[0]["id"]


def _status_map(
    client: TaigaClientWrapper,
    project_id: int,
    entity_type: str,
    session_id: str,
) -> Dict[int, Dict[str, Any]]:
    """Return a {status_id: {"name", "is_closed"}} map for an entity type, cached per session.

    Shares the per-session status cache with `_resolve_status` (same cache key), so a
    search that resolves statuses and a later name→id resolution reuse one API call.
    Returns an empty map for entity types that have no status resource.
    """
    resource = _STATUS_RESOURCE_MAP.get(entity_type)
    if not resource:
        return {}
    statuses = core._cached(
        session_id,
        f"statuses_{entity_type}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    return {
        s["id"]: {"name": s.get("name"), "is_closed": s.get("is_closed", False)} for s in statuses
    }


def _resolve_user(
    client: TaigaClientWrapper,
    project_id: int,
    username: str,
    session_id: str,
) -> int:
    """Resolve an email or full name to a user ID within project members.

    Resolution runs against ``/memberships``, which exposes no username field —
    see ``resolve_user_id``. The parameter name is historical.
    """
    members = core._cached(
        session_id,
        f"members_{project_id}",
        lambda: client.list_resources("memberships", project_id=project_id),
    )
    return resolve_user_id(members, username)


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

    return core._cached(session_id, f"issue_defaults_{project_id}", _fetch)


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
    items = core._cached(
        session_id,
        f"{resource}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    matches = [i for i in items if safe_lower(i.get("name")) == safe_lower(name)]
    if not matches:
        available = [i["name"] for i in items]
        raise ValueError(f"{attr_type.capitalize()} '{name}' not found. Available: {available}")
    return matches[0]["id"]


def _name_for_issue_attribute(
    client: TaigaClientWrapper,
    project_id: int,
    attr_type: str,
    attr_id: Optional[int],
    session_id: str,
) -> Optional[str]:
    """Resolve an issue priority/severity/type ID back to its name.

    Taiga exposes NO ``priority_extra_info`` / ``severity_extra_info`` /
    ``type_extra_info`` on its issue serializer — the only ``*_extra_info``
    objects it returns are ``assigned_to``, ``owner``, ``project`` and
    ``status``. These three fields come back as bare integer IDs, so reading a
    ``*_extra_info`` key for them silently yields None on every call and makes a
    correctly-set field look unset. Reverse-map the ID instead.

    Reads the same session cache that ``_resolve_issue_attribute`` populates, so
    this costs no extra API request after the first lookup per project.
    """
    if attr_id is None:
        return None
    resource = _ISSUE_ATTR_RESOURCE_MAP.get(attr_type)
    if not resource:
        raise ValueError(f"Unknown attribute type '{attr_type}'.")
    items = core._cached(
        session_id,
        f"{resource}_{project_id}",
        lambda: client.list_resources(resource, project_id=project_id),
    )
    return next((i.get("name") for i in items if i.get("id") == attr_id), None)


def _get_by_ref(
    client: TaigaClientWrapper, segment: str, ref: int, project_id: int
) -> Optional[Dict[str, Any]]:
    """Fetch an item by ref via ``/{segment}/by_ref``; None when it does not exist.

    Real Taiga answers an unknown ref with a 404 (it never returns an empty body),
    so a bare ``if not item`` after ``api.get`` is dead code. Only the 404 is
    translated: a 500 or an auth failure still surfaces.
    """
    try:
        return client.api.get(f"/{segment}/by_ref", params={"ref": ref, "project": project_id})
    except TaigaNotFoundError:
        return None


# Shared help text for the description arguments of update_epic/story/issue/task.
_DESCRIPTION_EDITS_HELP = (
    "description replaces the whole text; to change part of it, pass description_edits "
    "([{'old': ..., 'new': ...}], applied in order, each old must occur exactly once or "
    "nothing is written) and/or description_append / description_prepend (joined as-is, "
    "no newline added); the answer reports description_change. version: the version the "
    "item was read at (get_* returns it); if a field this call writes changed since, "
    "nothing is written (changes to other fields, comments included, do not block it). "
)


def _check_description_args(
    description: Optional[str],
    edits: Optional[List[Dict[str, str]]],
    append: Optional[str],
    prepend: Optional[str],
) -> None:
    """Validate the description arguments before any call.

    ``description`` replaces the whole text, so it cannot be combined with the
    partial forms. An empty ``edits`` list counts as not given.
    """
    partial = bool(edits) or append is not None or prepend is not None
    if description is not None and partial:
        raise ValueError(
            "description replaces the whole text: do not combine it with "
            "description_edits, description_append or description_prepend."
        )
    for i, edit in enumerate(edits or []):
        if (
            not isinstance(edit, dict)
            or set(edit) != {"old", "new"}
            or not all(isinstance(edit[k], str) for k in ("old", "new"))
        ):
            raise ValueError(
                f"description_edits[{i}] must be an object with exactly two string keys, "
                "'old' and 'new'."
            )
        if not edit["old"]:
            raise ValueError(f"description_edits[{i}].old is empty.")


def _edit_description(
    text: Optional[str],
    edits: Optional[List[Dict[str, str]]],
    append: Optional[str],
    prepend: Optional[str],
) -> tuple:
    """Apply exact replacements, then prepend/append, to a description.

    Edits apply in order, each to the text as the previous one left it, and each
    ``old`` must occur exactly once at that point; otherwise ValueError, so the
    caller writes nothing. Append and prepend are joined as-is (no newline added).
    Returns (new text, change report).
    """
    before = text or ""
    new = before
    for i, edit in enumerate(edits or []):
        old = edit["old"]
        first = new.find(old)
        # Overlapping matches count (search again from first + 1): str.count
        # would see "--|--" once in "--|--|--".
        if first < 0 or new.find(old, first + 1) >= 0:
            count = len(re.findall(f"(?={re.escape(old)})", new))
            found = "not found" if count == 0 else f"found {count} times"
            raise ValueError(
                f"description_edits[{i}].old must occur exactly once in the description, "
                f"{found}. Nothing was written."
            )
        new = new[:first] + edit["new"] + new[first + len(old) :]
    if prepend is not None:
        new = prepend + new
    if append is not None:
        new = new + append
    return new, {
        "replacements": len(edits or []),
        "prepended": prepend is not None,
        "appended": append is not None,
        "length_before": len(before),
        "length_after": len(new),
    }


def _description_payload(
    current: Dict[str, Any],
    description: Optional[str],
    edits: Optional[List[Dict[str, str]]],
    append: Optional[str],
    prepend: Optional[str],
) -> tuple:
    """(new description or None, change report or None) for an update tool."""
    if description is not None:
        before = current.get("description") or ""
        return description, {
            "replaced_whole_text": True,
            "length_before": len(before),
            "length_after": len(description),
        }
    if edits or append is not None or prepend is not None:
        return _edit_description(current.get("description"), edits, append, prepend)
    return None, None


def _base_version(current: Dict[str, Any], version: Optional[int]) -> int:
    """The version to PATCH with: the caller's when given, else the one just read.

    Taiga's own check is field-aware: a stale version is refused only when a field
    being written changed since. So a description edit against version V goes
    through after a mere comment or status change, and is refused when the
    description itself moved on, the one case where the edits may be wrong.
    """
    return current["version"] if version is None else version


def _patch_versioned(client: TaigaClientWrapper, path: str, payload: Dict[str, Any]):
    """PATCH with OCC; Taiga's version-mismatch 400 becomes an explicit conflict.

    Taiga refuses a stale ``version`` only when a field changed since that version
    is also being written, and answers 400 ``{"version": ...}`` (raised as
    TaigaConcurrencyError). The conflict is reported, never retried: a retry would
    overwrite the other change.
    """
    try:
        return client.api.patch(path, json=payload)
    except TaigaConcurrencyError as e:
        raise ValueError(
            f"Version conflict: a field this update writes changed since version "
            f"{payload.get('version')} (Taiga: {e.error_detail}). Nothing was written; "
            "read the item again and redo the change."
        ) from e


def _resolve_item_by_ref(client: TaigaClientWrapper, project: Any, entity_type: str, ref: int):
    """(project dict, item dict, REST segment) for an item by ref, or ValueError."""
    if entity_type not in _ENTITY_PATH_MAP:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_ENTITY_PATH_MAP)}."
        )
    proj = _resolve_project(client, project)
    segment = _ENTITY_PATH_MAP[entity_type]
    item = _get_by_ref(client, segment, ref, proj["id"])
    if not item:
        raise ValueError(f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'.")
    return proj, item, segment


def _story_filters(
    client: TaigaClientWrapper,
    proj: Dict[str, Any],
    session_id: str,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    epic: Optional[int] = None,
    tags: Optional[str] = None,
) -> Dict[str, Any]:
    """Build /userstories list query params from name-based filters.

    Shared by browse_backlog and list_items. Falsy status/assignee/tags are
    ignored (as browse_backlog always did); epic is used whenever not None.
    """
    project_id = proj["id"]
    filters: Dict[str, Any] = {}
    if status:
        filters["status"] = _resolve_status(client, project_id, "story", status, session_id)
    if assignee:
        filters["assigned_to"] = _resolve_user(client, project_id, assignee, session_id)
    if epic is not None:
        epic_data = _get_by_ref(client, "epics", epic, project_id)
        if not epic_data:
            raise ValueError(f"Epic #{epic} not found in project '{proj['slug']}'.")
        filters["epic"] = epic_data["id"]
    if tags:
        filters["tags"] = tags
    return filters


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


def _is_closed(item: Dict[str, Any]) -> bool:
    """Closed-ness of a work item, from the top-level field or its status row.

    Taiga's user-story serializer carries a top-level `is_closed`, but task and
    issue payloads don't always. `status_extra_info.is_closed` is the same
    property on the status row itself — the authoritative source `search` uses —
    so fall back to it when the top-level field is absent.
    """
    val = item.get("is_closed")
    if val is None:
        val = (item.get("status_extra_info") or {}).get("is_closed", False)
    return bool(val)


def _epic_summary(epic: Dict[str, Any]) -> Dict[str, Any]:
    """Extract readable fields from a raw epic dict (list rows, update results)."""
    return {
        "ref": epic.get("ref"),
        "subject": epic.get("subject"),
        "status": (epic.get("status_extra_info") or {}).get("name") or epic.get("status"),
        "assignee": (epic.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "color": epic.get("color"),
        "is_blocked": epic.get("is_blocked", False),
        "is_closed": _is_closed(epic),
        "tags": epic.get("tags", []),
    }


def _sprint_summary(milestone: Dict[str, Any]) -> Dict[str, Any]:
    """Extract readable fields from a raw milestone (sprint) dict."""
    return {
        "id": milestone.get("id"),
        "name": milestone.get("name"),
        "start": milestone.get("estimated_start"),
        "end": milestone.get("estimated_finish"),
        "is_closed": milestone.get("closed", False),
        "total_points": milestone.get("total_points"),
        "closed_points": milestone.get("closed_points"),
    }


def _story_summary(us: Dict[str, Any]) -> Dict[str, Any]:
    """Extract readable fields from a raw user story dict."""
    return {
        "ref": us.get("ref"),
        "subject": us.get("subject"),
        "status": (us.get("status_extra_info") or {}).get("name") or us.get("status"),
        "assignee": (us.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "is_blocked": us.get("is_blocked", False),
        "is_closed": _is_closed(us),
        # Taiga publishes NO milestone_extra_info on user stories — the sprint
        # arrives as `milestone` (ID) plus flat `milestone_name`/`milestone_slug`.
        "sprint": us.get("milestone_name") or us.get("milestone_slug"),
        "tags": us.get("tags", []),
    }


def _task_summary(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref": task.get("ref"),
        "subject": task.get("subject"),
        "status": (task.get("status_extra_info") or {}).get("name") or task.get("status"),
        "assignee": (task.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "is_blocked": task.get("is_blocked", False),
        "is_closed": _is_closed(task),
    }


def _issue_summary(
    issue: Dict[str, Any],
    client: Optional[TaigaClientWrapper] = None,
    project_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract readable fields from a raw issue dict (resolved names, not raw ids).

    priority/severity/type arrive as bare integer IDs — Taiga publishes no
    ``*_extra_info`` for them (see _name_for_issue_attribute). Pass client,
    project_id and session_id to get their names; without that context the raw
    IDs are returned rather than a misleading None.

    The three context arguments are all-or-nothing: supplying a subset raises
    rather than silently degrading to raw IDs, which would look like a successful
    resolution to the caller.
    """
    context = (client, project_id, session_id)
    if any(v is not None for v in context) and not all(v is not None for v in context):
        raise ValueError(
            "_issue_summary: client, project_id and session_id must be passed together "
            "(or all omitted)."
        )
    if all(v is not None for v in context):
        # Fall back to the raw ID when an ID is absent from the project's table,
        # mirroring the `status` line below — an unresolvable value should degrade
        # to something identifiable, not vanish.
        names = {
            attr: _name_for_issue_attribute(client, project_id, attr, issue.get(attr), session_id)
            or issue.get(attr)
            for attr in ("priority", "severity", "type")
        }
    else:
        names = {attr: issue.get(attr) for attr in ("priority", "severity", "type")}
    return {
        "ref": issue.get("ref"),
        "subject": issue.get("subject"),
        "status": (issue.get("status_extra_info") or {}).get("name") or issue.get("status"),
        "assignee": (issue.get("assigned_to_extra_info") or {}).get("full_name_display"),
        "priority": names["priority"],
        "severity": names["severity"],
        "type": names["type"],
        "is_blocked": issue.get("is_blocked", False),
        "is_closed": _is_closed(issue),
        "tags": issue.get("tags", []),
    }
