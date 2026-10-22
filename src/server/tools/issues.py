"""Issue tools."""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve

logger = logging.getLogger(__name__)


@app.mcp.tool(
    "get_issue",
    description=(
        "Read a single issue by its ref number. Returns resolved human fields "
        "(status name, is_closed, assignee, priority, severity, type, tags) plus the "
        "full description — no mutation. project accepts slug or ID."
    ),
)
def get_issue(
    project: Union[str, int],
    ref: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_get():
        proj = resolve._resolve_project(client, project)
        issue = client.api.get("/issues/by_ref", params={"ref": ref, "project": proj["id"]})
        if not issue:
            raise ValueError(f"Issue #{ref} not found in project '{proj['slug']}'.")
        # Single-item read: include the full description. Kept off the shared
        # _issue_summary shape for consistency with get_story/get_task, whose
        # helpers are reused by lean board/list composites.
        result = resolve._issue_summary(issue, client, proj["id"], actual_session_id)
        result["description"] = issue.get("description")
        return result

    return core._execute_taiga_operation("get_issue", do_get, f"#{ref} in {project}")


@app.mcp.tool(
    "create_issue",
    description=(
        "Create a new issue. type, priority, and severity default to the project's first configured value "
        "when not provided. All name fields are resolved (project slug, assignee email/full name). "
        "project accepts slug or ID."
    ),
)
def create_issue(
    project: Union[str, int],
    subject: str,
    description: Optional[str] = None,
    issue_type: Optional[str] = None,
    priority: Optional[str] = None,
    severity: Optional[str] = None,
    assignee: Optional[str] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_create():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        defaults = resolve._resolve_issue_defaults(client, project_id, actual_session_id)

        payload: Dict[str, Any] = {"project": project_id, "subject": subject}
        # Apply project defaults only when the project actually has them configured.
        # Sending None would produce a 400 from Taiga.
        for field in ("priority", "severity", "type"):
            if defaults.get(field) is not None:
                payload[field] = defaults[field]

        if description:
            payload["description"] = description
        if tags:
            payload["tags"] = tags
        if issue_type:
            payload["type"] = resolve._resolve_issue_attribute(
                client, project_id, "type", issue_type, actual_session_id
            )
        if priority:
            payload["priority"] = resolve._resolve_issue_attribute(
                client, project_id, "priority", priority, actual_session_id
            )
        if severity:
            payload["severity"] = resolve._resolve_issue_attribute(
                client, project_id, "severity", severity, actual_session_id
            )
        if assignee:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )

        # Default status = first configured issue status, when available.
        statuses = core._cached(
            actual_session_id,
            f"statuses_issue_{project_id}",
            lambda: client.list_resources("issue_statuses", project_id=project_id),
        )
        if statuses:
            payload["status"] = statuses[0]["id"]

        data = {k: v for k, v in payload.items() if k not in ("project", "subject")}
        result = client.api.post(
            "/issues", json={"project": project_id, "subject": subject, **data}
        )
        return {
            "status": "created",
            "ref": result.get("ref"),
            "id": result.get("id"),
            "subject": result.get("subject"),
            # `or result.get(...)` degrades an unresolvable ID to the raw integer
            # rather than None — see _issue_summary.
            "type": resolve._name_for_issue_attribute(
                client, project_id, "type", result.get("type"), actual_session_id
            )
            or result.get("type"),
            "priority": resolve._name_for_issue_attribute(
                client, project_id, "priority", result.get("priority"), actual_session_id
            )
            or result.get("priority"),
            "severity": resolve._name_for_issue_attribute(
                client, project_id, "severity", result.get("severity"), actual_session_id
            )
            or result.get("severity"),
        }

    return core._execute_taiga_operation("create_issue", do_create, str(project))


@app.mcp.tool(
    "update_issue",
    description=(
        "Update an issue by ref number. All fields optional. "
        "status, priority, severity, type, and assignee are resolved by name. "
        "project accepts slug or ID."
    ),
)
def update_issue(
    project: Union[str, int],
    ref: int,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    priority: Optional[str] = None,
    severity: Optional[str] = None,
    issue_type: Optional[str] = None,
    blocked: Optional[bool] = None,
    tags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_update():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        current = client.api.get("/issues/by_ref", params={"ref": ref, "project": project_id})
        if not current:
            raise ValueError(f"Issue #{ref} not found in project '{proj['slug']}'.")

        payload: Dict[str, Any] = {"version": current["version"]}
        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = resolve._resolve_status(
                client, project_id, "issue", status, actual_session_id
            )
        if assignee is not None:
            payload["assigned_to"] = resolve._resolve_user(
                client, project_id, assignee, actual_session_id
            )
        if priority is not None:
            payload["priority"] = resolve._resolve_issue_attribute(
                client, project_id, "priority", priority, actual_session_id
            )
        if severity is not None:
            payload["severity"] = resolve._resolve_issue_attribute(
                client, project_id, "severity", severity, actual_session_id
            )
        if issue_type is not None:
            payload["type"] = resolve._resolve_issue_attribute(
                client, project_id, "type", issue_type, actual_session_id
            )
        if blocked is not None:
            payload["is_blocked"] = blocked
        if tags is not None:
            payload["tags"] = tags

        if len(payload) == 1:
            raise ValueError("No fields to update were provided.")

        version = payload["version"]
        data = {k: v for k, v in payload.items() if k != "version"}
        result = client.api.patch(f"/issues/{current['id']}", json={"version": version, **data})
        return {
            "ref": result.get("ref"),
            "subject": result.get("subject"),
            "status": (result.get("status_extra_info") or {}).get("name"),
            # Raw-ID fallback on unresolvable IDs — see _issue_summary.
            "priority": resolve._name_for_issue_attribute(
                client, project_id, "priority", result.get("priority"), actual_session_id
            )
            or result.get("priority"),
            "severity": resolve._name_for_issue_attribute(
                client, project_id, "severity", result.get("severity"), actual_session_id
            )
            or result.get("severity"),
            "type": resolve._name_for_issue_attribute(
                client, project_id, "type", result.get("type"), actual_session_id
            )
            or result.get("type"),
            "assignee": (result.get("assigned_to_extra_info") or {}).get("full_name_display"),
            "is_blocked": result.get("is_blocked"),
            "is_closed": result.get("is_closed", False),
        }

    return core._execute_taiga_operation("update_issue", do_update, f"#{ref} in {project}")
