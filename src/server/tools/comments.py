"""Comment tools."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import _NO_PAGINATION_HEADERS, TaigaAuthenticationError, TaigaNotFoundError

logger = logging.getLogger(__name__)


# entity_type -> REST path segment (also the by_ref route prefix).
_COMMENT_PATH_MAP = resolve._ENTITY_PATH_MAP


# ---------------------------------------------------------------------------
# Comment tool
# ---------------------------------------------------------------------------


@app.tool(
    "add_comment",
    description=(
        "Add a comment to a user story, task, issue, or epic identified by ref number. "
        "entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "project accepts slug or ID."
    ),
)
def add_comment(
    project: Union[str, int],
    ref: int,
    text: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    if entity_type not in _COMMENT_PATH_MAP:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_COMMENT_PATH_MAP)}."
        )
    if not text or not text.strip():
        raise ValueError("Comment text must not be empty.")
    text = text.replace("\\n", "\n").replace("\\t", "\t")

    def do_comment():
        _, current, _ = _resolve_item(client, project, entity_type, ref)
        client.api.patch(
            f"/{_COMMENT_PATH_MAP[entity_type]}/{current['id']}",
            json={"comment": text, "version": current["version"]},
        )
        return {"status": "comment_added", "entity_type": entity_type, "ref": ref}

    return core._execute_taiga_operation(
        "add_comment", do_comment, f"{entity_type} #{ref} in {project}"
    )


# ---------------------------------------------------------------------------
# Reading, editing and history (v3 B6, GitHub #154)
# ---------------------------------------------------------------------------

# REST segment -> /history/<segment> name. Taiga's history routes use the
# singular model name, not the REST collection name.
_HISTORY_SEGMENT = {
    "userstories": "userstory",
    "tasks": "task",
    "issues": "issue",
    "epics": "epic",
}


def _resolve_item(client, project, entity_type: str, ref: int):
    """(project dict, item dict, history segment) for an item by ref, or ValueError."""
    proj, item, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
    return proj, item, _HISTORY_SEGMENT[segment]


def _author(entry: Dict[str, Any]) -> Optional[str]:
    user = entry.get("user") or {}
    return user.get("name") or user.get("full_name_display") or user.get("username")


def _comment_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": entry.get("id"),
        "author": _author(entry),
        "created_at": entry.get("created_at"),
        "comment": entry.get("comment"),
        "edited_at": entry.get("edit_comment_date"),
        "deleted_at": entry.get("delete_comment_date"),
    }


def _comment_id(comment_id: str) -> str:
    if not comment_id or not str(comment_id).strip():
        raise ValueError("comment_id must not be empty (take it from list_comments).")
    return str(comment_id).strip()


def _comment_action(
    client,
    hist: str,
    item_id: int,
    action: str,
    comment_id: str,
    label: str,
    json: Optional[Dict[str, Any]] = None,
) -> None:
    """POST /history/<hist>/<id>/<action>?id=<comment_id>; a 404 means no such comment."""
    try:
        client.api.post(f"/history/{hist}/{item_id}/{action}", params={"id": comment_id}, json=json)
    except TaigaNotFoundError:
        raise ValueError(f"Comment {comment_id} not found on {label} (see list_comments).")


def _fetch_history(client, hist: str, item_id: int) -> List[Dict[str, Any]]:
    """Full history of an item. /history is paginated (30 per page) like any list
    endpoint, so without the header an item with a long history loses its oldest
    entries, comments included."""
    return client.api.get(f"/history/{hist}/{item_id}", headers=_NO_PAGINATION_HEADERS) or []


def _run_comment_action(
    tool: str,
    action: str,
    status: str,
    project: Union[str, int],
    ref: int,
    comment_id: str,
    entity_type: str,
    session_id: Optional[str],
    json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Shared body of edit_comment / delete_comment / restore_comment."""
    comment_id = _comment_id(comment_id)
    client = core._get_authenticated_client(core._get_session_id(session_id))

    def do_action():
        _, item, hist = _resolve_item(client, project, entity_type, ref)
        _comment_action(client, hist, item["id"], action, comment_id, f"{entity_type} #{ref}", json)
        return {"status": status, "entity_type": entity_type, "ref": ref, "comment_id": comment_id}

    return core._execute_taiga_operation(
        tool, do_action, f"{entity_type} #{ref} comment {comment_id} in {project}"
    )


@app.tool(
    "list_comments",
    description=(
        "Read the comments of a user story, task, issue, or epic, in full and oldest first. "
        "Each comment has id (needed by edit_comment/delete_comment/restore_comment), author, "
        "created_at, the full text, edited_at and deleted_at. Deleted comments are left out "
        "unless include_deleted=True. entity_type: 'story' (default), 'task', 'issue', or "
        "'epic'. project accepts slug or ID."
    ),
)
def list_comments(
    project: Union[str, int],
    ref: int,
    entity_type: str = "story",
    include_deleted: bool = False,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_list():
        _, item, hist = _resolve_item(client, project, entity_type, ref)
        history = _fetch_history(client, hist, item["id"])
        comments = [
            _comment_summary(e)
            for e in history
            if (e.get("comment") or "").strip()
            and (include_deleted or not e.get("delete_comment_date"))
        ]
        # Taiga returns history newest first; a conversation reads oldest first.
        return sorted(comments, key=lambda c: c["created_at"] or "")

    return core._execute_taiga_operation(
        "list_comments", do_list, f"{entity_type} #{ref} in {project}"
    )


@app.tool(
    "edit_comment",
    description=(
        "Replace the text of an existing comment. comment_id comes from list_comments; "
        "Taiga keeps the earlier text as a version (see get_history with "
        "include_comment_versions=True). entity_type: 'story' (default), 'task', 'issue', "
        "or 'epic'. project accepts slug or ID."
    ),
)
def edit_comment(
    project: Union[str, int],
    ref: int,
    comment_id: str,
    text: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("Comment text must not be empty.")
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    return _run_comment_action(
        "edit_comment",
        "edit_comment",
        "comment_edited",
        project,
        ref,
        comment_id,
        entity_type,
        session_id,
        {"comment": text},
    )


@app.tool(
    "delete_comment",
    description=(
        "Delete a comment (soft delete: restore_comment brings it back, text included). "
        "comment_id comes from list_comments. entity_type: 'story' (default), 'task', "
        "'issue', or 'epic'. project accepts slug or ID."
    ),
)
def delete_comment(
    project: Union[str, int],
    ref: int,
    comment_id: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_comment_action(
        "delete_comment",
        "delete_comment",
        "comment_deleted",
        project,
        ref,
        comment_id,
        entity_type,
        session_id,
    )


@app.tool(
    "restore_comment",
    description=(
        "Restore a comment removed with delete_comment. Find its id with "
        "list_comments(include_deleted=True). entity_type: 'story' (default), 'task', "
        "'issue', or 'epic'. project accepts slug or ID."
    ),
)
def restore_comment(
    project: Union[str, int],
    ref: int,
    comment_id: str,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_comment_action(
        "restore_comment",
        "undelete_comment",
        "comment_restored",
        project,
        ref,
        comment_id,
        entity_type,
        session_id,
    )


@app.tool(
    "get_history",
    description=(
        "Change history (audit trail) of a user story, task, issue, or epic, newest first: "
        "who changed which fields when, plus comments. Each entry has id, created_at, author, "
        "changed_fields, and comment details when the entry is a comment. With "
        "include_comment_versions=True, edited comments also carry their earlier versions "
        "(one extra request per edited comment). entity_type: 'story' (default), 'task', "
        "'issue', or 'epic'. project accepts slug or ID."
    ),
)
def get_history(
    project: Union[str, int],
    ref: int,
    entity_type: str = "story",
    include_comment_versions: bool = False,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_history():
        _, item, hist = _resolve_item(client, project, entity_type, ref)
        history = _fetch_history(client, hist, item["id"])
        entries = []
        for e in history:
            entry: Dict[str, Any] = {
                "id": e.get("id"),
                "created_at": e.get("created_at"),
                "author": _author(e),
                # *_html keys duplicate their text field rendered as HTML.
                "changed_fields": sorted(
                    k for k in (e.get("diff") or {}) if not k.endswith("_html")
                ),
            }
            if (e.get("comment") or "").strip():
                entry["comment"] = e["comment"]
                entry["comment_edited_at"] = e.get("edit_comment_date")
                entry["comment_deleted_at"] = e.get("delete_comment_date")
                if include_comment_versions and e.get("edit_comment_date"):
                    try:
                        versions = (
                            client.api.get(
                                f"/history/{hist}/{item['id']}/comment_versions",
                                params={"id": e["id"]},
                            )
                            or []
                        )
                    except TaigaAuthenticationError as err:
                        # Taiga shows versions only to the comment's author and
                        # project admins; one forbidden comment must not hide
                        # the rest of the history.
                        if err.status_code != 403:
                            raise
                        entry["comment_versions"] = None
                        entry["comment_versions_error"] = "forbidden (author or admin only)"
                    else:
                        entry["comment_versions"] = [
                            {
                                "date": v.get("date"),
                                "author": _author(v),
                                "comment": v.get("comment"),
                            }
                            for v in versions
                        ]
            entries.append(entry)
        return entries

    return core._execute_taiga_operation(
        "get_history", do_history, f"{entity_type} #{ref} in {project}"
    )
