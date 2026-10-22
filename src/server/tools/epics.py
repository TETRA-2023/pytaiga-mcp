"""Epic tools."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import (
    TaigaException,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Epic tools
# ---------------------------------------------------------------------------


@app.mcp.tool(
    "get_epic_overview",
    description=(
        "Return an epic (including its full description) and all user stories linked "
        "to it, with per-status counts. ref is the epic ref number. project accepts "
        "slug or ID."
    ),
)
def get_epic_overview(
    project: Union[str, int],
    ref: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_overview():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        epic = client.api.get("/epics/by_ref", params={"ref": ref, "project": project_id})
        if not epic:
            raise ValueError(f"Epic #{ref} not found in project '{proj['slug']}'.")

        # Single batched call instead of one /user_stories/<id> per linked story.
        linked_stories = client.list_resources(
            "user_stories", project_id=project_id, epic=epic["id"]
        )

        stories = [resolve._story_summary(s) for s in linked_stories]
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
                "description": epic.get("description"),
            },
            "summary": {
                "total_stories": len(stories),
                "stories_by_status": by_status,
            },
            "stories": stories,
        }

    return core._execute_taiga_operation("get_epic_overview", do_overview, f"#{ref} in {project}")


@app.mcp.tool(
    "create_epic",
    description=(
        "Create a new epic and optionally link existing user stories to it by ref number. "
        "assignee accepts an email or full name. story_refs is a list of existing US ref numbers to link. "
        "project accepts slug or ID."
    ),
)
def create_epic(
    project: Union[str, int],
    subject: str,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    color: Optional[str] = None,
    story_refs: Optional[List[int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_create():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        if description:
            payload["description"] = description
        if color:
            payload["color"] = color
        if assignee:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )

        epic = client.api.post("/epics", json=payload)
        epic_id = epic["id"]

        linked = 0
        if story_refs:
            # One POST per story. Do NOT use /related_userstories/bulk_create: it
            # creates NEW stories from newline-separated subjects, so a list of ids
            # became one junk story and nothing was linked (pytaiga-mcp#143).
            us_ids = resolve._resolve_story_refs(client, project_id, story_refs)
            failed = []
            for ref, us_id in zip(story_refs, us_ids):
                try:
                    client.api.post(
                        f"/epics/{epic_id}/related_userstories",
                        json={"epic": epic_id, "user_story": us_id},
                    )
                    linked += 1
                except TaigaException as e:
                    failed.append(f"#{ref}: {e}")
            if failed:
                raise TaigaException(
                    f"Epic #{epic.get('ref')} (id {epic_id}) was created, but linked only "
                    f"{linked} of {len(us_ids)} stories. Failed: {'; '.join(failed)}"
                )

        return {
            "status": "created",
            "ref": epic.get("ref"),
            "id": epic_id,
            "subject": epic.get("subject"),
            "stories_linked": linked,
        }

    return core._execute_taiga_operation("create_epic", do_create, str(project))
