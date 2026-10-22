"""Epic tools."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import _NO_PAGINATION_HEADERS, TaigaBadRequestError, TaigaException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Epic tools
# ---------------------------------------------------------------------------


def _link_stories(client, epic_id: int, refs_and_ids: List[tuple]) -> tuple:
    """Link stories to an epic, one POST per story.

    Returns (linked refs, already-linked refs, failure messages). Never
    /related_userstories/bulk_create: it creates NEW stories from
    newline-separated subjects, so a list of ids became one junk story and
    nothing was linked (pytaiga-mcp#143).
    """
    linked, already, failed = [], [], []
    for ref, us_id in refs_and_ids:
        try:
            client.api.post(
                f"/epics/{epic_id}/related_userstories",
                json={"epic": epic_id, "user_story": us_id},
            )
            linked.append(ref)
        except TaigaBadRequestError as e:
            # Linked meanwhile by someone else (Taiga: "Related user story with
            # this User story and Epic already exists."): not a failure.
            if "already exists" in e.error_detail:
                already.append(ref)
            else:
                failed.append(f"#{ref}: {e}")
        except TaigaException as e:
            failed.append(f"#{ref}: {e}")
    return linked, already, failed


@app.tool(
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

        epic = resolve._get_by_ref(client, "epics", ref, project_id)
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
            "epic": {**resolve._epic_summary(epic), "description": epic.get("description")},
            "summary": {
                "total_stories": len(stories),
                "stories_by_status": by_status,
            },
            "stories": stories,
        }

    return core._execute_taiga_operation("get_epic_overview", do_overview, f"#{ref} in {project}")


@app.tool(
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
            us_ids = resolve._resolve_story_refs(client, project_id, story_refs)
            done, already, failed = _link_stories(client, epic_id, list(zip(story_refs, us_ids)))
            linked = len(done) + len(already)
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


@app.tool(
    "update_epic",
    description=(
        "Update an epic by its ref number. All fields are optional — only provided fields "
        "change. status and assignee are resolved by name; color is a hex string "
        "(e.g. '#A5694F'); tags replaces the epic's tags. Linking stories is not done "
        "here: use create_epic(story_refs=...) or update_story(epic=...). "
        "project accepts slug or ID."
    ),
)
def update_epic(
    project: Union[str, int],
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    color: Optional[str] = None,
    tags: Optional[List[str]] = None,
    blocked: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    fields = (subject, description, status, assignee, color, tags, blocked)
    if all(v is None for v in fields):
        raise ValueError("No fields to update were provided.")
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_update():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        current = resolve._get_by_ref(client, "epics", ref, project_id)
        if not current:
            raise ValueError(f"Epic #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": current["version"]}
        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = resolve._resolve_status(
                client, project_id, "epic", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )
        if color is not None:
            payload["color"] = color
        if tags is not None:
            payload["tags"] = tags
        if blocked is not None:
            payload["is_blocked"] = blocked

        result = client.api.patch(f"/epics/{current['id']}", json=payload)
        return resolve._epic_summary(result)

    return core._execute_taiga_operation("update_epic", do_update, f"epic #{ref} in {project}")


@app.tool(
    "link_to_epic",
    description=(
        "Link existing user stories (by ref) to an existing epic (by ref). Stories already "
        "linked to it are reported in already_linked and left alone; a story may belong to "
        "several epics. If some links fail, the error says which were linked and which not. "
        "project accepts slug or ID."
    ),
)
def link_to_epic(
    project: Union[str, int],
    epic_ref: int,
    story_refs: List[int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    refs = list(dict.fromkeys(story_refs or []))  # dedupe, keep order
    if not refs:
        raise ValueError("story_refs must contain at least one story ref.")
    client = core._get_authenticated_client(core._get_session_id(session_id))

    def do_link():
        proj, epic, _ = resolve._resolve_item_by_ref(client, project, "epic", epic_ref)
        us_ids = resolve._resolve_story_refs(client, proj["id"], refs)
        existing = {
            r.get("user_story")
            for r in client.api.get(
                f"/epics/{epic['id']}/related_userstories", headers=_NO_PAGINATION_HEADERS
            )
            or []
        }
        todo = [(ref, us_id) for ref, us_id in zip(refs, us_ids) if us_id not in existing]
        already = [ref for ref, us_id in zip(refs, us_ids) if us_id in existing]
        linked, raced, failed = _link_stories(client, epic["id"], todo)
        already += raced
        if failed:
            already_note = f"; already linked: {already}" if already else ""
            raise TaigaException(
                f"Linked {linked or 'none'} to epic #{epic_ref}; failed: {'; '.join(failed)}"
                f"{already_note}"
            )
        return {
            "status": "linked",
            "epic_ref": epic_ref,
            "linked": linked,
            "already_linked": already,
        }

    return core._execute_taiga_operation(
        "link_to_epic", do_link, f"{len(refs)} stories to epic #{epic_ref} in {project}"
    )
