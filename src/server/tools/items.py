"""Tools that act on any item type (entity_type=story|task|issue|epic)."""

import logging
from typing import Any, Dict, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)


@app.mcp.tool(
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
