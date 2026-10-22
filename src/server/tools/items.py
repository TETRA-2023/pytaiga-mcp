"""Tools that act on several item types, selected by an entity_type argument."""

import logging
from typing import Any, Dict, List, Optional, Union

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

    # entity_type -> REST path segment (also the by_ref route prefix).
    type_map = resolve._ENTITY_PATH_MAP
    if entity_type not in type_map:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(type_map)}."
        )

    def do_assign():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        user_id = resolve._resolve_user(client, project_id, username, actual_session_id)

        segment = type_map[entity_type]
        current = client.api.get(f"/{segment}/by_ref", params={"ref": ref, "project": project_id})
        if not current:
            raise ValueError(f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'.")

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
# real Taiga (tests/test_integration.py).
_LIST_FILTERS = {
    "story": {"status", "sprint", "epic", "tags", "assignee"},
    "epic": {"status", "assignee"},
    "sprint": {"status"},
}


@app.tool(
    "list_items",
    description=(
        "List every user story, epic, or sprint of a project, with optional filters. "
        "entity_type: 'story' (default; ALL stories, sprint-assigned ones included — "
        "browse_backlog returns only those in no sprint), 'epic', or 'sprint'. "
        "Filters by type — story: status (name), sprint (name or ID), epic (ref), "
        "tags (comma-separated), assignee (email/full name); epic: status (name), assignee; "
        "sprint: status 'open' or 'closed'. A filter that does not apply to the type is an "
        "error. project accepts slug or ID."
    ),
)
def list_items(
    project: Union[str, int],
    entity_type: str = "story",
    status: Optional[str] = None,
    sprint: Optional[Union[str, int]] = None,
    epic: Optional[int] = None,
    tags: Optional[str] = None,
    assignee: Optional[str] = None,
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
        )
        if value is not None
    }
    unsupported = given - _LIST_FILTERS[entity_type]
    if unsupported:
        raise ValueError(
            f"Filter(s) {sorted(unsupported)} do not apply to entity_type '{entity_type}'. "
            f"Supported: {sorted(_LIST_FILTERS[entity_type])}."
        )
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_list():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        filters: Dict[str, Any] = {}

        if entity_type == "sprint":
            if status is not None:
                if status.lower() not in ("open", "closed"):
                    raise ValueError("Sprint status must be 'open' or 'closed'.")
                filters["closed"] = status.lower() == "closed"
            sprints = client.list_resources("milestones", project_id=project_id, **filters)
            return [resolve._sprint_summary(m) for m in sprints]

        if entity_type == "epic":
            if status is not None:
                filters["status"] = resolve._resolve_status(
                    client, project_id, "epic", status, actual_session_id
                )
            if assignee is not None:
                filters["assigned_to"] = resolve._resolve_user(
                    client, project_id, assignee, actual_session_id
                )
            epics = client.list_resources("epics", project_id=project_id, **filters)
            return [resolve._epic_summary(e) for e in epics]

        filters = resolve._story_filters(
            client, proj, actual_session_id, status=status, assignee=assignee, epic=epic, tags=tags
        )
        if sprint is not None:
            filters["milestone"] = resolve._resolve_sprint(client, project_id, sprint)["id"]
        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        return [resolve._story_summary(s) for s in stories]

    return core._execute_taiga_operation("list_items", do_list, f"{entity_type} list in {project}")


# entity_type -> (REST segment, how the item is identified). Behaviour on delete,
# measured on taiga.tetra-ai.fr (2026-10-22), is stated in the tool description.
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
