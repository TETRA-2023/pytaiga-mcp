"""Wiki tools."""

import logging
from typing import Any, Dict, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import (
    TaigaNotFoundError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wiki tools
# ---------------------------------------------------------------------------


@app.tool(
    "get_wiki",
    description=(
        "Get a wiki page by slug, or list all wiki pages when slug is omitted. "
        "project accepts slug or ID."
    ),
)
def get_wiki(
    project: Union[str, int],
    slug: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Any:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_get():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        if slug:
            page = client.api.get("/wiki/by_slug", params={"slug": slug, "project": project_id})
            if not page:
                raise ValueError(f"Wiki page '{slug}' not found in project '{proj['slug']}'.")
            return {"slug": page.get("slug"), "content": page.get("content"), "id": page.get("id")}

        pages = client.list_resources("wiki", project_id=project_id)
        return [{"slug": p.get("slug"), "id": p.get("id")} for p in pages]

    return core._execute_taiga_operation("get_wiki", do_get, str(project))


@app.tool(
    "upsert_wiki",
    description=(
        "Create or update a wiki page. If a page with the given slug already exists it is updated; "
        "otherwise a new page is created. project accepts slug or ID."
    ),
)
def upsert_wiki(
    project: Union[str, int],
    slug: str,
    content: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_upsert():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        # `/wiki/by_slug` RAISES 404 for an unknown slug — it does not return a falsy
        # value. Without this guard the exception escaped do_upsert and the `else`
        # branch below was unreachable, so upsert_wiki could only ever UPDATE an
        # existing page and never create one, despite its description. Catch the
        # not-found case specifically so a 500 / auth failure still surfaces.
        try:
            existing = client.api.get("/wiki/by_slug", params={"slug": slug, "project": project_id})
        except TaigaNotFoundError:
            existing = None
        if existing:
            result = client.api.patch(
                f"/wiki/{existing['id']}",
                json={"version": existing["version"], "slug": slug, "content": content},
            )
            return {"status": "updated", "slug": result.get("slug"), "id": result.get("id")}
        else:
            result = client.api.post(
                "/wiki", json={"project": project_id, "slug": slug, "content": content}
            )
            return {"status": "created", "slug": result.get("slug"), "id": result.get("id")}

    return core._execute_taiga_operation("upsert_wiki", do_upsert, str(project))
