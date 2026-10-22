"""Bulk tools: create many items in one call, reorder stories inside one column."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)

# entity_type -> (bulk_create endpoint, payload key). taiga-back
# UserStoriesBulkValidator / IssuesBulkValidator / EpicsBulkValidator read the
# subjects as ONE newline-separated string.
_BULK_CREATE = {
    "story": ("/userstories/bulk_create", "bulk_stories"),
    "user_story": ("/userstories/bulk_create", "bulk_stories"),
    "issue": ("/issues/bulk_create", "bulk_issues"),
    "epic": ("/epics/bulk_create", "bulk_epics"),
}
_MAX_BULK = 100


@app.tool(
    "create_items",
    description=(
        "Create several user stories, issues, or epics in one call, from a list of subjects "
        f"(at most {_MAX_BULK}; a subject may not contain a line break). They get the "
        "project's default status (and, for issues, default type/priority/severity); edit "
        "them afterwards with update_story / update_issue / update_epic. For tasks under a "
        "story use break_down_story. entity_type: 'story' (default), 'issue', or 'epic'. "
        "project accepts slug or ID."
    ),
)
def create_items(
    project: Union[str, int],
    subjects: List[str],
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if entity_type not in _BULK_CREATE:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_BULK_CREATE)}."
        )
    cleaned = [s.strip() for s in subjects or [] if s and s.strip()]
    if not cleaned:
        raise ValueError("subjects must contain at least one non-empty subject.")
    if len(cleaned) > _MAX_BULK:
        raise ValueError(f"At most {_MAX_BULK} subjects per call, got {len(cleaned)}.")
    multiline = [s for s in cleaned if "\n" in s or "\r" in s]
    if multiline:
        # Taiga splits the joined string on line breaks: one subject would
        # silently become several items.
        raise ValueError(f"Subjects may not contain line breaks: {multiline[:3]}")
    endpoint, key = _BULK_CREATE[entity_type]
    client = core._get_authenticated_client(core._get_session_id(session_id))

    def do_create():
        proj = resolve._resolve_project(client, project)
        created = (
            client.api.post(endpoint, json={"project_id": proj["id"], key: "\n".join(cleaned)})
            or []
        )
        return {
            "status": "created",
            "entity_type": entity_type,
            "count": len(created),
            "items": [
                {"ref": c.get("ref"), "id": c.get("id"), "subject": c.get("subject")}
                for c in created
            ],
        }

    return core._execute_taiga_operation(
        "create_items", do_create, f"{len(cleaned)} {entity_type}(s) in {project}"
    )


def _column_of(story: Dict[str, Any], view: str):
    """The column a story sits in for a given view: its sprint, or its kanban cell."""
    if view == "kanban":
        return (story.get("status"), story.get("swimlane"))
    return story.get("milestone")


@app.tool(
    "reorder_stories",
    description=(
        "Reorder user stories inside ONE column, as drag-and-drop does. view: 'backlog' "
        "(stories in no sprint), 'sprint' (needs sprint name or ID), or 'kanban' (stories "
        "sharing one status). story_refs are placed, in that order, right before before_ref "
        "or right after after_ref (another story of the same column), or at the top when "
        "neither is given. Every story must already be in that column: this tool never moves "
        "stories between sprints or statuses — use move_to_sprint / set_story_status for that. "
        "project accepts slug or ID."
    ),
)
def reorder_stories(
    project: Union[str, int],
    story_refs: List[int],
    view: str = "backlog",
    sprint: Optional[Union[str, int]] = None,
    before_ref: Optional[int] = None,
    after_ref: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    refs = list(dict.fromkeys(story_refs or []))
    if not refs:
        raise ValueError("story_refs must contain at least one story ref.")
    if view not in ("backlog", "sprint", "kanban"):
        raise ValueError("view must be 'backlog', 'sprint', or 'kanban'.")
    if view == "sprint" and sprint is None:
        raise ValueError("view='sprint' needs the sprint (name or ID).")
    if view != "sprint" and sprint is not None:
        raise ValueError("sprint only applies to view='sprint'.")
    if before_ref is not None and after_ref is not None:
        raise ValueError("Give before_ref or after_ref, not both.")
    anchor = before_ref if before_ref is not None else after_ref
    if anchor is not None and anchor in refs:
        raise ValueError("The anchor story cannot be one of the stories being moved.")
    client = core._get_authenticated_client(core._get_session_id(session_id))

    def do_reorder():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        stories = {
            s["ref"]: s for s in client.list_resources("user_stories", project_id=project_id)
        }
        checked = refs + ([anchor] if anchor is not None else [])
        missing = [r for r in checked if r not in stories]
        if missing:
            raise ValueError(f"User stories not found: {missing}")

        if view == "sprint":
            column = resolve._resolve_sprint(client, project_id, sprint)["id"]
        elif view == "backlog":
            column = None
        else:
            cells = {_column_of(stories[r], view) for r in refs}
            if len(cells) != 1:
                raise ValueError(
                    "For view='kanban' all stories must share one status (and swimlane)."
                )
            column = cells.pop()

        # The ordering endpoints also SET the column on every moved story (and
        # its tasks' sprint): a story from elsewhere would be silently moved.
        outside = [r for r in checked if _column_of(stories[r], view) != column]
        if outside:
            where = {
                "backlog": "the backlog",
                "sprint": f"sprint {sprint!r}",
                "kanban": "that kanban column",
            }[view]
            raise ValueError(
                f"Stories {outside} are not in {where}; reorder_stories only reorders inside one "
                "column (use move_to_sprint or set_story_status to move stories)."
            )

        payload: Dict[str, Any] = {
            "project_id": project_id,
            "bulk_userstories": [stories[r]["id"] for r in refs],
        }
        if before_ref is not None:
            payload["before_userstory_id"] = stories[before_ref]["id"]
        if after_ref is not None:
            payload["after_userstory_id"] = stories[after_ref]["id"]
        if view == "kanban":
            status_id, swimlane_id = column
            payload["status_id"] = status_id
            if swimlane_id is not None:
                payload["swimlane_id"] = swimlane_id
            endpoint = "/userstories/bulk_update_kanban_order"
        else:
            if view == "sprint":
                payload["milestone_id"] = column
            endpoint = "/userstories/bulk_update_backlog_order"
        client.api.post(endpoint, json=payload)
        return {
            "status": "reordered",
            "view": view,
            "story_refs": refs,
            "before_ref": before_ref,
            "after_ref": after_ref,
        }

    return core._execute_taiga_operation(
        "reorder_stories", do_reorder, f"{len(refs)} stories ({view}) in {project}"
    )
