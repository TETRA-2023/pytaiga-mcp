"""Comment tools."""

import logging
from typing import Any, Dict, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)


# entity_type -> REST path segment (also the by_ref route prefix).
_COMMENT_PATH_MAP = resolve._ENTITY_PATH_MAP


# ---------------------------------------------------------------------------
# Comment tool
# ---------------------------------------------------------------------------


@app.mcp.tool(
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
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        path_segment = _COMMENT_PATH_MAP[entity_type]

        # path_segment ("userstories"/"tasks"/"issues"/"epics") doubles as the
        # by_ref route prefix.
        current = client.api.get(
            f"/{path_segment}/by_ref", params={"ref": ref, "project": project_id}
        )
        if not current:
            raise ValueError(f"{entity_type.capitalize()} #{ref} not found in '{proj['slug']}'.")

        client.api.patch(
            f"/{path_segment}/{current['id']}",
            json={"comment": text, "version": current["version"]},
        )
        return {"status": "comment_added", "entity_type": entity_type, "ref": ref}

    return core._execute_taiga_operation(
        "add_comment", do_comment, f"{entity_type} #{ref} in {project}"
    )
