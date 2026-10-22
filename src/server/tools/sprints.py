"""Sprint tools: board, planning, moves, closing."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sprint tools
# ---------------------------------------------------------------------------


@app.mcp.tool(
    "get_sprint_board",
    description=(
        "Return a full sprint board: sprint metadata, all user stories with their tasks, "
        "and a status summary. sprint accepts a name or ID; omit to use the current sprint "
        "(single open milestone covering today). with_tasks=True (default) fetches tasks per story. "
        "project accepts slug or ID."
    ),
)
def get_sprint_board(
    project: Union[str, int],
    sprint: Optional[Union[str, int]] = None,
    with_tasks: bool = True,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_board():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        milestone = resolve._resolve_sprint(client, project_id, sprint)
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
            row = resolve._story_summary(s)
            if with_tasks:
                row["tasks"] = [resolve._task_summary(t) for t in tasks_by_story.get(s["id"], [])]
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

    return core._execute_taiga_operation("get_sprint_board", do_board, str(project))


@app.mcp.tool(
    "plan_sprint",
    description=(
        "Create a new sprint and optionally assign user stories to it in one step. "
        "story_refs is a list of user story ref numbers (e.g. [12, 15, 23]). "
        "Dates must be ISO 8601 (YYYY-MM-DD). project accepts slug or ID."
    ),
)
def plan_sprint(
    project: Union[str, int],
    name: str,
    start_date: str,
    end_date: str,
    story_refs: Optional[List[int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_plan():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        milestone = client.api.post(
            "/milestones",
            json={
                "project": project_id,
                "name": name,
                "estimated_start": start_date,
                "estimated_finish": end_date,
            },
        )
        milestone_id = milestone["id"]

        stories_moved = 0
        if story_refs:
            us_ids = resolve._resolve_story_refs(client, project_id, story_refs)
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

    return core._execute_taiga_operation("plan_sprint", do_plan, str(project))


@app.mcp.tool(
    "move_to_sprint",
    description=(
        "Move one or more user stories (by ref number) into a sprint. "
        "sprint accepts a name or ID. story_refs is a list of ref numbers. "
        "project accepts slug or ID."
    ),
)
def move_to_sprint(
    project: Union[str, int],
    story_refs: List[int],
    sprint: Union[str, int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_move():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        milestone = resolve._resolve_sprint(client, project_id, sprint)

        us_ids = resolve._resolve_story_refs(client, project_id, story_refs)
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

    return core._execute_taiga_operation("move_to_sprint", do_move, str(project))


@app.mcp.tool(
    "close_sprint",
    description=(
        "Mark a sprint as closed. sprint accepts a name or ID; "
        "omit to close the current sprint (single open milestone covering today). "
        "project accepts slug or ID."
    ),
)
def close_sprint(
    project: Union[str, int],
    sprint: Optional[Union[str, int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_close():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        milestone = resolve._resolve_sprint(client, project_id, sprint)

        result = client.api.patch(
            f"/milestones/{milestone['id']}",
            json={"closed": True, "version": milestone.get("version", 1)},
        )
        return {
            "status": "closed",
            "sprint": result.get("name"),
            "id": result.get("id"),
        }

    return core._execute_taiga_operation("close_sprint", do_close, str(project))
