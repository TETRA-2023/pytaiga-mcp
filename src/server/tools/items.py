"""Tools that act on several item types, selected by an entity_type argument."""

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Union

import pydantic_core

from src.server import app, core, resolve
from src.taiga_client import TaigaNotFoundError

logger = logging.getLogger(__name__)


@app.tool(
    "assign_item",
    description=(
        "Assign a user story, task, issue, or epic to a team member by email or full name. "
        "The `username` parameter name is historical — Taiga memberships expose no "
        "username, so pass an email address or the member's full name. "
        "entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "ref is the item ref number. project accepts slug or ID."
    ),
)
def assign_item(
    project: Union[str, int],
    ref: int,
    username: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_assign():
        proj, current, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
        user_id = resolve._resolve_user(client, proj["id"], username, actual_session_id)

        result = client.api.patch(
            f"/{segment}/{current['id']}",
            json={"version": current["version"], "assigned_to": user_id},
        )
        return {
            "ref": ref,
            "entity_type": entity_type,
            "assigned_to": (result.get("assigned_to_extra_info") or {}).get("full_name_display")
            or username,
        }

    return core._execute_taiga_operation(
        "assign_item", do_assign, f"{entity_type} #{ref} in {project}"
    )


# Filters each list_items entity_type accepts. Taiga silently ignores a query
# param it doesn't know, so an unsupported filter must be rejected here, not
# passed through (it would return the unfiltered list). Each one is verified on
# real Taiga (tests/test_integration.py). Taiga IGNORES `is_blocked`, so
# `blocked` is applied here, after the read; a list of statuses goes as the
# comma-separated IDs Taiga's status filter takes; modified_before is
# `modified_date__lt`.
_LIST_FILTERS = {
    "story": {"status", "sprint", "epic", "tags", "assignee", "blocked", "modified_before"},
    "task": {"status", "assignee", "story", "closed", "blocked", "modified_before"},
    "epic": {"status", "assignee", "blocked"},
    "sprint": {"status"},
}

# list_items refuses a result whose text exceeds this many characters (the
# client would refuse it inline anyway), like get_project_activity's guard.
_LIST_MAX_CHARS = 50_000


def _rows_size(rows: List[Dict[str, Any]]) -> int:
    """Characters the client receives: FastMCP sends each row of a list result
    as its own indented JSON text block."""
    return sum(len(pydantic_core.to_json(r, fallback=str, indent=2).decode()) for r in rows)


def _guard_size(rows: List[Dict[str, Any]], entity_type: str) -> List[Dict[str, Any]]:
    """Return rows, or refuse them with what would narrow the list. A cut list
    would read as complete (a missing blocked task looks unblocked), so the
    guard never truncates."""
    size = _rows_size(rows)
    if size <= _LIST_MAX_CHARS:
        return rows
    by_status = Counter(str(r.get("status")) for r in rows)
    breakdown = ", ".join(f"{name} {n}" for name, n in by_status.most_common())
    blocked = sum(1 for r in rows if r.get("is_blocked"))
    narrow = sorted(_LIST_FILTERS[entity_type])
    raise ValueError(
        f"list_items: {len(rows)} {entity_type} rows ({size:,} characters) exceed the "
        f"{_LIST_MAX_CHARS:,}-character limit; nothing returned. By status: {breakdown}; "
        f"blocked: {blocked}. Narrow with: {', '.join(narrow)} "
        f"(status takes a list of names)."
    )


# structured_output=False: with an output schema FastMCP sends the rows twice
# (text blocks + structuredContent). The text blocks alone carry them, and the
# size guard measures those.
@app.tool(
    "list_items",
    structured_output=False,
    description=(
        "List every user story, task, epic, or sprint of a project, with optional filters. "
        "entity_type: 'story' (default; ALL stories, sprint-assigned ones included — "
        "browse_backlog returns only those in no sprint), 'task', 'epic', or 'sprint'. "
        "Filters by type — story: status, sprint (name or ID), epic (ref), "
        "tags (comma-separated), assignee (email/full name), blocked, modified_before; "
        "task: status, assignee, story (parent story ref), closed (true/false), blocked, "
        "modified_before; epic: status, assignee, blocked; sprint: status 'open' or "
        "'closed'. status is a name or a list of names (any of them matches). "
        "blocked=true keeps only blocked items, false only unblocked ones. "
        "modified_before (ISO date or datetime; a date or a time without offset is UTC) "
        "keeps items last modified before that moment, e.g. 'In progress' untouched for a "
        "week. A filter that does not apply to the type is an error. Story, task and epic "
        "rows carry modified_date, and blocked_note when blocked. A result over 50,000 "
        "characters is refused with an error giving the row count, a breakdown by status "
        "and the filters to narrow it; it is never cut. project accepts slug or ID."
    ),
)
def list_items(
    project: Union[str, int],
    entity_type: str = "story",
    status: Optional[Union[str, List[str]]] = None,
    sprint: Optional[Union[str, int]] = None,
    epic: Optional[int] = None,
    tags: Optional[str] = None,
    assignee: Optional[str] = None,
    story: Optional[int] = None,
    closed: Optional[bool] = None,
    blocked: Optional[bool] = None,
    modified_before: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if entity_type not in _LIST_FILTERS:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_LIST_FILTERS)}."
        )
    given = {
        name
        for name, value in (
            ("status", status),
            ("sprint", sprint),
            ("epic", epic),
            ("tags", tags),
            ("assignee", assignee),
            ("story", story),
            ("closed", closed),
            ("blocked", blocked),
            ("modified_before", modified_before),
        )
        if value is not None
    }
    unsupported = given - _LIST_FILTERS[entity_type]
    if unsupported:
        raise ValueError(
            f"Filter(s) {sorted(unsupported)} do not apply to entity_type '{entity_type}'. "
            f"Supported: {sorted(_LIST_FILTERS[entity_type])}."
        )
    before = (
        resolve._parse_moment(modified_before, "modified_before")
        if modified_before is not None
        else None
    )
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def keep(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if blocked is not None:
            rows = [r for r in rows if bool(r.get("is_blocked")) == blocked]
        return _guard_size(rows, entity_type)

    def do_list():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        filters: Dict[str, Any] = {}

        if entity_type == "sprint":
            if status is not None:
                if not isinstance(status, str) or status.lower() not in ("open", "closed"):
                    raise ValueError("Sprint status must be 'open' or 'closed'.")
                filters["closed"] = status.lower() == "closed"
            sprints = client.list_resources("milestones", project_id=project_id, **filters)
            return _guard_size([resolve._sprint_summary(m) for m in sprints], entity_type)

        if entity_type == "epic":
            if status is not None:
                filters["status"] = resolve._resolve_statuses(
                    client, project_id, "epic", status, actual_session_id
                )
            if assignee is not None:
                filters["assigned_to"] = resolve._resolve_user(
                    client, project_id, assignee, actual_session_id
                )
            epics = client.list_resources("epics", project_id=project_id, **filters)
            return keep([resolve._epic_summary(e) for e in epics])

        if entity_type == "task":
            if status is not None:
                filters["status"] = resolve._resolve_statuses(
                    client, project_id, "task", status, actual_session_id
                )
            if assignee is not None:
                filters["assigned_to"] = resolve._resolve_user(
                    client, project_id, assignee, actual_session_id
                )
            if story is not None:
                parent = resolve._get_by_ref(client, "userstories", story, project_id)
                if not parent:
                    raise ValueError(f"User story #{story} not found in '{proj['slug']}'.")
                filters["user_story"] = parent["id"]
            if closed is not None:
                filters["status__is_closed"] = closed
        else:
            filters = resolve._story_filters(
                client,
                proj,
                actual_session_id,
                status=status,
                assignee=assignee,
                epic=epic,
                tags=tags,
            )
            if sprint is not None:
                filters["milestone"] = resolve._resolve_sprint(client, project_id, sprint)["id"]
        if before is not None:
            filters["modified_date__lt"] = before.isoformat()

        if entity_type == "task":
            tasks = client.list_resources("tasks", project_id=project_id, **filters)
            return keep([resolve._task_summary(t) for t in tasks])
        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        return keep([resolve._story_summary(s) for s in stories])

    return core._execute_taiga_operation("list_items", do_list, f"{entity_type} list in {project}")


# entity_type -> (REST segment, how the item is identified). Behaviour on delete,
# measured on a live Taiga 6 (2026-10-22), is stated in the tool description.
_DELETE_TYPES = {
    **{t: (segment, "ref") for t, segment in resolve._ENTITY_PATH_MAP.items()},
    "sprint": ("milestones", "name"),
    "wiki": ("wiki", "slug"),
}


def _is_ref(value: Any) -> bool:
    """A ref number: a non-bool int, or a string of digits."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, str) and value.isdigit())


@app.tool(
    "delete_item",
    description=(
        "PERMANENTLY delete one item — Taiga has no undo. "
        "entity_type: 'story', 'task', 'issue', 'epic' (identified by ref number), "
        "'sprint' (name or ID), or 'wiki' (page slug). What else is affected: "
        "deleting a story ALSO DELETES ITS TASKS; deleting a sprint keeps its stories and "
        "tasks (they return to the backlog); deleting an epic keeps its stories (only the "
        "links go). Returns what was deleted (subject, sprint name or wiki slug) so a wrong "
        "ref is visible at once. project accepts slug or ID."
    ),
)
def delete_item(
    project: Union[str, int],
    entity_type: str,
    ref_or_name: Union[int, str],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if entity_type not in _DELETE_TYPES:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_DELETE_TYPES)}."
        )
    segment, key = _DELETE_TYPES[entity_type]
    if key == "ref" and not _is_ref(ref_or_name):
        raise ValueError(f"A {entity_type} is identified by its ref number, got {ref_or_name!r}.")
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_delete():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        if key == "ref":
            ref = int(ref_or_name)
            item = resolve._get_by_ref(client, segment, ref, project_id)
            if not item:
                raise ValueError(
                    f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'."
                )
            deleted = {"ref": ref, "subject": item.get("subject")}
        elif key == "name":
            item = resolve._resolve_sprint(client, project_id, ref_or_name)
            deleted = {"name": item.get("name")}
        else:
            try:
                item = client.api.get(
                    "/wiki/by_slug", params={"slug": ref_or_name, "project": project_id}
                )
            except TaigaNotFoundError:
                item = None
            if not item:
                raise ValueError(f"Wiki page '{ref_or_name}' not found in '{proj['slug']}'.")
            deleted = {"slug": item.get("slug")}

        client.api.delete(f"/{segment}/{item['id']}")
        logger.info(f"Deleted {entity_type} {deleted} (id {item['id']}) in {proj['slug']}")
        return {"status": "deleted", "entity_type": entity_type, "id": item["id"], **deleted}

    return core._execute_taiga_operation(
        "delete_item", do_delete, f"{entity_type} {ref_or_name!r} in {project}"
    )
