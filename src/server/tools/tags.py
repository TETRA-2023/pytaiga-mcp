"""Project tag tools: list, rename/recolor, merge."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)


@app.tool(
    "list_tags",
    description=(
        "List a project's tags with their colors (hex string, or null when uncolored), "
        "sorted by name. project accepts slug or ID."
    ),
)
def list_tags(
    project: Union[str, int],
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_list():
        proj = resolve._resolve_project(client, project)
        colors = client.api.get(f"/projects/{proj['id']}/tags_colors") or {}
        return [{"tag": t, "color": c} for t, c in sorted(colors.items())]

    return core._execute_taiga_operation("list_tags", do_list, str(project))


@app.tool(
    "rename_tag",
    description=(
        "Rename a project tag and/or change its color, everywhere it is used. Give new_name, "
        "color (hex like '#A5694F'), or both; an omitted one is kept. Taiga rejects an "
        "unknown tag or a new_name already in use. Requires PROJECT ADMIN rights. "
        "project accepts slug or ID."
    ),
)
def rename_tag(
    project: Union[str, int],
    tag: str,
    new_name: Optional[str] = None,
    color: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    tag = (tag or "").strip()
    new_name = (new_name or "").strip() or None
    if not tag:
        raise ValueError("tag must not be empty.")
    if new_name is None and color is None:
        raise ValueError("Give new_name, color, or both.")
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_rename():
        proj = resolve._resolve_project(client, project)
        payload: Dict[str, Any] = {"from_tag": tag}
        if new_name is not None:
            payload["to_tag"] = new_name
        if color is not None:
            payload["color"] = color
        with core._admin_required("rename_tag", proj["slug"]):
            client.api.post(f"/projects/{proj['id']}/edit_tag", json=payload)
        final = new_name or tag
        # Read the color back: an omitted color is kept by Taiga, and returning
        # None here would read as "uncolored" (list_tags uses null for that).
        colors = client.api.get(f"/projects/{proj['id']}/tags_colors") or {}
        return {"status": "tag_updated", "tag": tag, "new_name": final, "color": colors.get(final)}

    return core._execute_taiga_operation("rename_tag", do_rename, f"'{tag}' in {project}")


@app.tool(
    "merge_tags",
    description=(
        "Merge tags: every item tagged with any of from_tags is retagged with to_tag, and "
        "from_tags are removed from the project. All tags must already exist. Requires "
        "PROJECT ADMIN rights. project accepts slug or ID."
    ),
)
def merge_tags(
    project: Union[str, int],
    from_tags: List[str],
    to_tag: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    to_tag = (to_tag or "").strip()
    sources = [t.strip() for t in from_tags or [] if t and t.strip()]
    if not to_tag:
        raise ValueError("to_tag must not be empty.")
    if not sources:
        raise ValueError("from_tags must contain at least one tag.")
    if to_tag in sources:
        raise ValueError("to_tag must not be one of from_tags.")
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_merge():
        proj = resolve._resolve_project(client, project)
        with core._admin_required("merge_tags", proj["slug"]):
            client.api.post(
                f"/projects/{proj['id']}/mix_tags", json={"from_tags": sources, "to_tag": to_tag}
            )
        return {"status": "tags_merged", "from_tags": sources, "to_tag": to_tag}

    return core._execute_taiga_operation("merge_tags", do_merge, f"into '{to_tag}' in {project}")
