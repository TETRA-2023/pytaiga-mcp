"""User story tools."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import TaigaException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-by-ref tools (pure inspection: status, assignee, … resolved to names)
# ---------------------------------------------------------------------------


@app.tool(
    "get_story",
    description=(
        "Read a single user story by its ref number. Returns resolved human fields "
        "(status name, is_closed, assignee, sprint, tags) plus the full description "
        "— no mutation. project accepts slug or ID."
    ),
)
def get_story(
    project: Union[str, int],
    ref: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_get():
        proj = resolve._resolve_project(client, project)
        story = resolve._get_by_ref(client, "userstories", ref, proj["id"])
        if not story:
            raise ValueError(f"User story #{ref} not found in project '{proj['slug']}'.")
        # Single-item read: include the full description. The shared _story_summary
        # helper stays lean (it is reused by list/board composites where the
        # description would bloat every row).
        result = resolve._story_summary(story)
        result["description"] = story.get("description")
        result["version"] = story.get("version")
        return result

    return core._execute_taiga_operation("get_story", do_get, f"#{ref} in {project}")


# ---------------------------------------------------------------------------
# Backlog tools
# ---------------------------------------------------------------------------


@app.tool(
    "browse_backlog",
    description=(
        "List the backlog: ONLY the user stories assigned to no sprint — not every story "
        "of the project (use list_items for all stories, sprint-assigned ones included). "
        "Filters: status (name), assignee (email/full name), epic (ref number), tags (comma-separated). "
        "project accepts slug or ID."
    ),
)
def browse_backlog(
    project: Union[str, int],
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    epic: Optional[int] = None,
    tags: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_browse():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        filters = resolve._story_filters(
            client, proj, actual_session_id, status=status, assignee=assignee, epic=epic, tags=tags
        )
        filters["milestone"] = "null"  # backlog = no sprint

        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        return [resolve._story_summary(s) for s in stories]

    return core._execute_taiga_operation("browse_backlog", do_browse, str(project))


@app.tool(
    "create_story",
    description=(
        "Create a user story in the backlog with optional epic link, sprint assignment, and assignee. "
        "project accepts slug or ID. assignee accepts an email or full name. "
        "sprint accepts name or ID (omit to place in backlog). epic accepts ref number."
    ),
)
def create_story(
    project: Union[str, int],
    subject: str,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Union[str, int]] = None,
    epic: Optional[int] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_create():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        # Validate the epic BEFORE creating the story. Create and epic-link are two
        # separate Taiga calls, so resolving the epic first means a bad ref fails
        # cleanly instead of leaving an orphaned (created-but-unlinked) story behind.
        epic_data = None
        if epic is not None:
            epic_data = resolve._get_by_ref(client, "epics", epic, project_id)
            if not epic_data:
                raise ValueError(f"Epic #{epic} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        if description:
            payload["description"] = description
        if tags:
            payload["tags"] = tags
        if assignee:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )
        if sprint is not None:
            milestone = resolve._resolve_sprint(client, project_id, sprint)
            payload["milestone"] = milestone["id"]

        result = client.api.post("/userstories", json=payload)

        # Link to the already-validated epic.
        if epic_data is not None and result:
            client.api.post(
                f"/epics/{epic_data['id']}/related_userstories",
                json={"epic": epic_data["id"], "user_story": result["id"]},
            )

        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            # No milestone_extra_info on user stories — see _story_summary.
            "sprint": result.get("milestone_name") or result.get("milestone_slug"),
            "epic_linked": epic is not None,
        }

    return core._execute_taiga_operation("create_story", do_create, str(project))


@app.tool(
    "update_story",
    description=(
        "Update a user story by its ref number. All fields are optional — only provided fields are changed. "
        "status, assignee, and sprint are resolved by name. "
        "epic accepts an epic ref number to link the story to that epic, or 0 to unlink from all epics. "
        + resolve._DESCRIPTION_EDITS_HELP
        + "project accepts slug or ID."
    ),
)
def update_story(
    project: Union[str, int],
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    description_edits: Optional[List[Dict[str, str]]] = None,
    description_append: Optional[str] = None,
    description_prepend: Optional[str] = None,
    version: Optional[int] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Union[str, int]] = None,
    epic: Optional[int] = None,
    tags: Optional[List[str]] = None,
    blocked: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolve._check_description_args(
        description, description_edits, description_append, description_prepend
    )
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_update():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        current = resolve._get_by_ref(client, "userstories", ref, project_id)
        if not current:
            raise ValueError(f"User story #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": resolve._base_version(current, version)}
        if subject is not None:
            payload["subject"] = subject
        new_description, description_change = resolve._description_payload(
            current,
            description,
            description_edits,
            description_append,
            description_prepend,
        )
        if new_description is not None:
            payload["description"] = new_description
        if status is not None:
            payload["status"] = resolve._resolve_status(
                client, project_id, "story", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )
        if sprint is not None:
            milestone = resolve._resolve_sprint(client, project_id, sprint)
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

        # Validate the target epic BEFORE any mutation. If we deleted old links
        # first and the lookup then failed, the story would be left orphaned
        # despite the caller seeing an error.
        new_epic = None
        if epic:
            new_epic = resolve._get_by_ref(client, "epics", epic, project_id)
            if not new_epic:
                raise ValueError(f"Epic #{epic} not found in project '{proj['slug']}'.")

        # PATCH before relinking: a version conflict then leaves the links as they were.
        result = current
        if len(payload) > 1:
            result = resolve._patch_versioned(client, f"/userstories/{current['id']}", payload)

        epic_error = None
        if epic is not None:
            try:
                # Taiga's UserStorySerializer returns `epics` as a list of dicts
                # ({"id": <epic_id>, "ref": ..., "subject": ..., "color": ...}),
                # not bare IDs — extract `id` from each entry.
                for old_epic in current.get("epics") or []:
                    client.api.delete(
                        f"/epics/{old_epic['id']}/related_userstories/{current['id']}"
                    )
                if new_epic is not None:
                    client.api.post(
                        f"/epics/{new_epic['id']}/related_userstories",
                        json={"epic": new_epic["id"], "user_story": current["id"]},
                    )
                epic_changed = True
            except TaigaException as e:
                if result is current:  # nothing else was written: a plain failure
                    raise
                # The PATCH is saved; raising would make the caller retry it against
                # a version that has moved on. Report the relink failure instead.
                epic_error = (
                    f"The other fields were saved, but relinking to "
                    f"{f'epic #{epic}' if epic else 'no epic'} failed: {e}. "
                    "The story's epic links may be partly changed."
                )

        summary = resolve._story_summary(result)
        if epic_error:
            summary["epic_error"] = epic_error
        if epic_changed:
            summary["epic"] = epic if epic else None
        if description_change is not None:
            summary["description_change"] = {**description_change, "version": result.get("version")}
        return summary

    return core._execute_taiga_operation("update_story", do_update, f"#{ref} in {project}")


@app.tool(
    "set_story_status",
    description=(
        "Change the status of a user story by its ref number using the status name "
        "(e.g. 'In Progress', 'Done'). project accepts slug or ID."
    ),
)
def set_story_status(
    project: Union[str, int],
    ref: int,
    status: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_set():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        status_id = resolve._resolve_status(client, project_id, "story", status, actual_session_id)

        current = resolve._get_by_ref(client, "userstories", ref, project_id)
        if not current:
            raise ValueError(f"User story #{ref} not found in project '{proj['slug']}'.")

        result = client.api.patch(
            f"/userstories/{current['id']}",
            json={"status": status_id, "version": current["version"]},
        )
        return {
            "ref": ref,
            "status": (result.get("status_extra_info") or {}).get("name") or status,
            "is_closed": result.get("is_closed", False),
        }

    return core._execute_taiga_operation("set_story_status", do_set, f"#{ref} in {project}")
