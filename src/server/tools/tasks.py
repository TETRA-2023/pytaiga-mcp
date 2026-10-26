"""Task tools, including break_down_story."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)

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


@app.tool(
    "get_task",
    description=(
        "Read a single task by its ref number. Returns resolved human fields "
        "(status name, is_closed, assignee, parent story_ref) plus the full description "
        "— no mutation. "
        "project accepts slug or ID."
    ),
)
def get_task(
    project: Union[str, int],
    ref: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_get():
        proj = resolve._resolve_project(client, project)
        task = resolve._get_by_ref(client, "tasks", ref, proj["id"])
        if not task:
            raise ValueError(f"Task #{ref} not found in project '{proj['slug']}'.")
        # Single-item read: include the full description (see get_story rationale).
        result = resolve._task_summary(task)
        result["description"] = task.get("description")
        result["version"] = task.get("version")
        return result

    return core._execute_taiga_operation("get_task", do_get, f"task #{ref} in {project}")


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------


@app.tool(
    "create_task",
    description=(
        "Add a single task under an existing user story. story_ref is the parent US ref number. "
        "By default the task inherits the parent story's sprint; pass sprint=<name|id> to "
        "place the task in a different sprint, or sprint=0 to keep it out of any sprint. "
        "All name fields are resolved (assignee email/full name, status name, sprint name). "
        "project accepts slug or ID."
    ),
)
def create_task(
    project: Union[str, int],
    story_ref: int,
    subject: str,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[Union[str, int]] = None,
    due_date: Optional[str] = None,
    tags: Optional[List[str]] = None,
    blocked: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_create():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        story = resolve._get_by_ref(client, "userstories", story_ref, project_id)
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
            data["milestone"] = resolve._resolve_sprint(client, project_id, sprint)["id"]

        if description:
            data["description"] = description
        if tags:
            data["tags"] = tags
        if due_date:
            data["due_date"] = due_date
        if blocked is not None:
            data["is_blocked"] = blocked
        if status:
            data["status"] = resolve._resolve_status(
                client, project_id, "task", status, actual_session_id
            )
        if assignee:
            data["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )

        result = client.api.post("/tasks", json={"project": project_id, "subject": subject, **data})
        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            "parent_story_ref": story_ref,
        }

    return core._execute_taiga_operation(
        "create_task", do_create, f"under US #{story_ref} in {project}"
    )


@app.tool(
    "update_task",
    description=(
        "Update a task by its ref number. All fields are optional — only provided fields change. "
        "status, assignee, and sprint are resolved by name. story_ref reparents the task to a "
        "different user story. sprint accepts a name or ID; tasks can have a milestone "
        "independent of their parent story. "
        + resolve._DESCRIPTION_EDITS_HELP
        + "project accepts slug or ID."
    ),
)
def update_task(
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
    story_ref: Optional[int] = None,
    blocked: Optional[bool] = None,
    tags: Optional[List[str]] = None,
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

        current = resolve._get_by_ref(client, "tasks", ref, project_id)
        if not current:
            raise ValueError(f"Task #{ref} not found in project '{proj['slug']}'.")

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
                client, project_id, "task", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )
        if sprint is not None:
            milestone = resolve._resolve_sprint(client, project_id, sprint)
            payload["milestone"] = milestone["id"]
        if story_ref is not None:
            new_story = resolve._get_by_ref(client, "userstories", story_ref, project_id)
            if not new_story:
                raise ValueError(f"User story #{story_ref} not found in project '{proj['slug']}'.")
            payload["user_story"] = new_story["id"]
        if blocked is not None:
            payload["is_blocked"] = blocked
        if tags is not None:
            payload["tags"] = tags

        if len(payload) == 1:  # only version key
            raise ValueError("No fields to update were provided.")

        result = resolve._patch_versioned(client, f"/tasks/{current['id']}", payload)
        summary = resolve._task_summary(result)
        if description_change is not None:
            summary["description_change"] = {**description_change, "version": result.get("version")}
        return summary

    return core._execute_taiga_operation("update_task", do_update, f"task #{ref} in {project}")


@app.tool(
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
    project: Union[str, int],
    story_ref: int,
    tasks: List[Union[str, Dict[str, Any]]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not tasks:
        raise ValueError("tasks list cannot be empty.")
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_break_down():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        story = resolve._get_by_ref(client, "userstories", story_ref, project_id)
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
                    data["status"] = resolve._resolve_status(
                        client, project_id, "task", entry["status"], actual_session_id
                    )
                if "assignee" in entry:
                    data["assigned_to"] = resolve._resolve_user(
                        client, project_id, entry["assignee"], actual_session_id
                    )
                task = client.api.post(
                    "/tasks", json={"project": project_id, "subject": entry["subject"], **data}
                )
                created.append(task)

        return {
            "status": "decomposed",
            "story_ref": story_ref,
            "tasks_created": len(created),
            "tasks": [{"ref": t.get("ref"), "subject": t.get("subject")} for t in created],
        }

    return core._execute_taiga_operation(
        "break_down_story", do_break_down, f"US #{story_ref} in {project}"
    )


@app.tool(
    "set_task_status",
    description=(
        "Change the status of a task by its ref number using the status name "
        "(e.g. 'In Progress', 'Done'). project accepts slug or ID."
    ),
)
def set_task_status(
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
        status_id = resolve._resolve_status(client, project_id, "task", status, actual_session_id)

        current = resolve._get_by_ref(client, "tasks", ref, project_id)
        if not current:
            raise ValueError(f"Task #{ref} not found in project '{proj['slug']}'.")

        result = client.api.patch(
            f"/tasks/{current['id']}", json={"version": current["version"], "status": status_id}
        )
        return {
            "ref": ref,
            "status": (result.get("status_extra_info") or {}).get("name") or status,
            "is_closed": result.get("is_closed", False),
        }

    return core._execute_taiga_operation("set_task_status", do_set, f"task #{ref} in {project}")
