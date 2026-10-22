"""Project administration: settings (statuses, points, types, priorities, severities,
custom attributes), custom attribute values, members and invitations.

Writes to settings and invitations need PROJECT ADMIN rights in Taiga; their 403
is re-raised by core._admin_required with a message saying so. Listing settings
and members, and setting custom attribute values (a normal item edit), do not.
"""

import logging
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import _NO_PAGINATION_HEADERS

logger = logging.getLogger(__name__)

# kind -> (endpoint, bulk_update_order param, delete needs moveTo, editable fields).
# Endpoints and bulk params from taiga-back projects/api.py and
# custom_attributes/api.py. For the MoveOnDestroy kinds, Taiga refuses a DELETE
# without ?moveTo=<id> (400 "Query param 'moveTo' is required", even when unused)
# and moves every item, and the project default, to that replacement.
_KINDS: Dict[str, tuple] = {
    "story_statuses": ("userstory-statuses", "bulk_userstory_statuses", True, "status"),
    "task_statuses": ("task-statuses", "bulk_task_statuses", True, "status"),
    "issue_statuses": ("issue-statuses", "bulk_issue_statuses", True, "status"),
    "epic_statuses": ("epic-statuses", "bulk_epic_statuses", True, "status"),
    "issue_types": ("issue-types", "bulk_issue_types", True, "colored"),
    "priorities": ("priorities", "bulk_priorities", True, "colored"),
    "severities": ("severities", "bulk_severities", True, "colored"),
    "points": ("points", "bulk_points", True, "points"),
    "story_custom_attributes": (
        "userstory-custom-attributes",
        "bulk_userstory_custom_attributes",
        False,
        "attribute",
    ),
    "task_custom_attributes": (
        "task-custom-attributes",
        "bulk_task_custom_attributes",
        False,
        "attribute",
    ),
    "issue_custom_attributes": (
        "issue-custom-attributes",
        "bulk_issue_custom_attributes",
        False,
        "attribute",
    ),
    "epic_custom_attributes": (
        "epic-custom-attributes",
        "bulk_epic_custom_attributes",
        False,
        "attribute",
    ),
}
# Fields each family accepts. Taiga ignores unknown body fields silently, so an
# argument that doesn't apply to the kind is rejected here.
_FIELDS = {
    "status": {"color", "is_closed"},
    "colored": {"color"},
    "points": {"value"},
    "attribute": {"description", "field_type"},
}
_FIELD_TYPES = ("text", "multiline", "richtext", "date", "url", "dropdown", "checkbox", "number")
# entity_type -> custom attribute kind (for set_custom_attribute_values).
_ATTRIBUTE_KIND = {
    "story": "story_custom_attributes",
    "user_story": "story_custom_attributes",
    "task": "task_custom_attributes",
    "issue": "issue_custom_attributes",
    "epic": "epic_custom_attributes",
}


def _kind(kind: str) -> tuple:
    if kind not in _KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Must be one of: {', '.join(_KINDS)}.")
    return _KINDS[kind]


def _body(kind: str, **fields: Any) -> Dict[str, Any]:
    """Validate the optional fields for `kind` and map them to Taiga's names."""
    family = _KINDS[kind][3]
    given = {k: v for k, v in fields.items() if v is not None}
    unsupported = set(given) - _FIELDS[family]
    if unsupported:
        raise ValueError(
            f"Field(s) {sorted(unsupported)} do not apply to kind '{kind}'. "
            f"Supported: {sorted(_FIELDS[family])}."
        )
    if "field_type" in given:
        if given["field_type"] not in _FIELD_TYPES:
            raise ValueError(f"field_type must be one of: {', '.join(_FIELD_TYPES)}.")
        given["type"] = given.pop("field_type")
    return given


def _setting_summary(kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
    family = _KINDS[kind][3]
    out: Dict[str, Any] = {
        "id": item.get("id"),
        "name": item.get("name"),
        "order": item.get("order"),
    }
    if family in ("status", "colored"):
        out["color"] = item.get("color")
    if family == "status":
        out["is_closed"] = item.get("is_closed", False)
    if family == "points":
        out["value"] = item.get("value")
    if family == "attribute":
        out["description"] = item.get("description")
        out["field_type"] = item.get("type")
    return out


def _invalidate_lookups(sid: str) -> None:
    """Drop the session's cached lookup tables (statuses, priorities, ...) after a
    settings write, so resolvers like set_story_status see the new values.

    Takes the session id resolved at the start of the tool: resolving it again
    after the write could raise (default session logged out meanwhile) and
    report a failure for a write that went through.
    """
    core._clear_session_cache(sid)


def _list_settings(client, kind: str, project_id: int) -> List[Dict[str, Any]]:
    endpoint = _KINDS[kind][0]
    rows = client.api.get(
        f"/{endpoint}", params={"project": project_id}, headers=_NO_PAGINATION_HEADERS
    )
    return sorted(rows or [], key=lambda r: (r.get("order") or 0, r.get("id") or 0))


def _find_setting(rows: List[Dict[str, Any]], kind: str, name_or_id: Union[str, int]) -> Dict:
    """A setting by exact name (case-insensitive), else by id (int or digit string).

    Names are tried first: points are named "1", "3", "8"..., so treating an int
    as an id would pick the wrong entry when the caller means the name.
    """
    wanted = str(name_or_id).strip().lower()
    matches = [r for r in rows if (r.get("name") or "").strip().lower() == wanted]
    if not matches and not isinstance(name_or_id, bool) and wanted.isdigit():
        matches = [r for r in rows if r.get("id") == int(wanted)]
    if len(matches) == 1:
        return matches[0]
    names = [r.get("name") for r in rows]
    if not matches:
        raise ValueError(f"No {kind} entry named {name_or_id!r}. Existing: {names}")
    raise ValueError(f"Several {kind} entries are named {name_or_id!r}; pass the id instead.")


@app.tool(
    "list_project_settings",
    description=(
        "List a project's configurable values of one kind, in display order. kind: "
        "story_statuses, task_statuses, issue_statuses, epic_statuses (id, name, order, color, "
        "is_closed); issue_types, priorities, severities (id, name, order, color); points "
        "(id, name, order, value); story_custom_attributes, task_custom_attributes, "
        "issue_custom_attributes, epic_custom_attributes (id, name, order, description, "
        "field_type). Readable by any member. project accepts slug or ID."
    ),
)
def list_project_settings(
    project: Union[str, int],
    kind: str,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    _kind(kind)
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_list():
        proj = resolve._resolve_project(client, project)
        return [_setting_summary(kind, r) for r in _list_settings(client, kind, proj["id"])]

    return core._execute_taiga_operation("list_project_settings", do_list, f"{kind} in {project}")


@app.tool(
    "create_project_setting",
    description=(
        "Add a status, issue type, priority, severity, point value, or custom attribute to a "
        "project (see list_project_settings for the kinds). Optional fields by kind — statuses: "
        "color (hex), is_closed; issue_types/priorities/severities: color; points: value "
        "(number, or omit for '?'); custom attributes: description, field_type (text, "
        "multiline, richtext, date, url, dropdown, checkbox, number; default text). A field "
        "that doesn't apply to the kind is an error. Requires PROJECT ADMIN rights. "
        "project accepts slug or ID."
    ),
)
def create_project_setting(
    project: Union[str, int],
    kind: str,
    name: str,
    color: Optional[str] = None,
    is_closed: Optional[bool] = None,
    value: Optional[float] = None,
    description: Optional[str] = None,
    field_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint = _kind(kind)[0]
    name = (name or "").strip()
    if not name:
        raise ValueError("name must not be empty.")
    body = _body(
        kind,
        color=color,
        is_closed=is_closed,
        value=value,
        description=description,
        field_type=field_type,
    )
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_create():
        proj = resolve._resolve_project(client, project)
        with core._admin_required("create_project_setting", proj["slug"]):
            created = client.api.post(
                f"/{endpoint}", json={"project": proj["id"], "name": name, **body}
            )
        _invalidate_lookups(sid)
        return {"status": "created", "kind": kind, **_setting_summary(kind, created)}

    return core._execute_taiga_operation(
        "create_project_setting", do_create, f"{kind} '{name}' in {project}"
    )


@app.tool(
    "update_project_setting",
    description=(
        "Change one project setting, found by its current name (or id): rename it with "
        "new_name and/or change the fields its kind supports (statuses: color, is_closed; "
        "issue_types/priorities/severities: color; points: value; custom attributes: "
        "description, field_type). Requires PROJECT ADMIN rights. project accepts slug or ID."
    ),
)
def update_project_setting(
    project: Union[str, int],
    kind: str,
    name: Union[str, int],
    new_name: Optional[str] = None,
    color: Optional[str] = None,
    is_closed: Optional[bool] = None,
    value: Optional[float] = None,
    description: Optional[str] = None,
    field_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint = _kind(kind)[0]
    body = _body(
        kind,
        color=color,
        is_closed=is_closed,
        value=value,
        description=description,
        field_type=field_type,
    )
    new_name = (new_name or "").strip() or None
    if new_name:
        body["name"] = new_name
    if not body:
        raise ValueError("No fields to update were provided.")
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_update():
        proj = resolve._resolve_project(client, project)
        current = _find_setting(_list_settings(client, kind, proj["id"]), kind, name)
        with core._admin_required("update_project_setting", proj["slug"]):
            updated = client.api.patch(f"/{endpoint}/{current['id']}", json=body)
        _invalidate_lookups(sid)
        return {"status": "updated", "kind": kind, **_setting_summary(kind, updated)}

    return core._execute_taiga_operation(
        "update_project_setting", do_update, f"{kind} {name!r} in {project}"
    )


@app.tool(
    "delete_project_setting",
    description=(
        "PERMANENTLY delete one project setting, found by name (or id). For statuses, issue "
        "types, priorities, severities and points, move_to is REQUIRED (Taiga refuses "
        "otherwise): every item using the deleted value — and the project default, if it was "
        "the default — is switched to move_to. Deleting a custom attribute (no move_to) "
        "removes its values from every item. Requires PROJECT ADMIN rights. "
        "project accepts slug or ID."
    ),
)
def delete_project_setting(
    project: Union[str, int],
    kind: str,
    name: Union[str, int],
    move_to: Optional[Union[str, int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint, _, needs_move, _ = _kind(kind)
    if needs_move and move_to is None:
        raise ValueError(
            f"Deleting a {kind} entry needs move_to: the value its items switch to "
            "(see list_project_settings)."
        )
    if not needs_move and move_to is not None:
        raise ValueError(f"move_to does not apply to {kind}.")
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_delete():
        proj = resolve._resolve_project(client, project)
        rows = _list_settings(client, kind, proj["id"])
        target = _find_setting(rows, kind, name)
        params = None
        moved = None
        if needs_move:
            moved = _find_setting(rows, kind, move_to)
            if moved["id"] == target["id"]:
                raise ValueError("move_to must be a different entry than the one deleted.")
            params = {"moveTo": moved["id"]}
        with core._admin_required("delete_project_setting", proj["slug"]):
            client.api.delete(f"/{endpoint}/{target['id']}", params=params)
        _invalidate_lookups(sid)
        logger.info(f"Deleted {kind} '{target.get('name')}' (id {target['id']}) in {proj['slug']}")
        result = {"status": "deleted", "kind": kind, "id": target["id"], "name": target.get("name")}
        if moved:
            result["moved_to"] = moved.get("name")
        return result

    return core._execute_taiga_operation(
        "delete_project_setting", do_delete, f"{kind} {name!r} in {project}"
    )


@app.tool(
    "reorder_project_settings",
    description=(
        "Set the display order of one kind of project setting. names lists ALL entries of "
        "the kind (see list_project_settings) in the new order. Requires PROJECT ADMIN rights. "
        "project accepts slug or ID."
    ),
)
def reorder_project_settings(
    project: Union[str, int],
    kind: str,
    names: List[str],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint, bulk_param, _, _ = _kind(kind)
    if not names:
        raise ValueError("names must list the entries in their new order.")
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_reorder():
        proj = resolve._resolve_project(client, project)
        rows = _list_settings(client, kind, proj["id"])
        ordered = [_find_setting(rows, kind, n) for n in names]
        ids = [r["id"] for r in ordered]
        if len(set(ids)) != len(ids):
            raise ValueError("names lists the same entry twice.")
        missing = [r.get("name") for r in rows if r["id"] not in set(ids)]
        if missing:
            raise ValueError(f"names must list every {kind} entry; missing: {missing}")
        with core._admin_required("reorder_project_settings", proj["slug"]):
            client.api.post(
                f"/{endpoint}/bulk_update_order",
                json={"project": proj["id"], bulk_param: [[i, n] for n, i in enumerate(ids)]},
            )
        # Cached lists keep the old order, and issue defaults are the first entry.
        _invalidate_lookups(sid)
        return {"status": "reordered", "kind": kind, "order": [r.get("name") for r in ordered]}

    return core._execute_taiga_operation(
        "reorder_project_settings", do_reorder, f"{kind} in {project}"
    )


@app.tool(
    "set_custom_attribute_values",
    description=(
        "Set custom attribute values on a user story, task, issue, or epic. values maps "
        "attribute NAME to value (see list_project_settings with the matching "
        "*_custom_attributes kind); a null value clears that attribute (it then reads null). "
        "Other attributes keep their values. entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "project accepts slug or ID."
    ),
)
def set_custom_attribute_values(
    project: Union[str, int],
    ref: int,
    values: Dict[str, Any],
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if entity_type not in _ATTRIBUTE_KIND:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(_ATTRIBUTE_KIND)}."
        )
    if not values:
        raise ValueError("values must name at least one attribute.")
    kind = _ATTRIBUTE_KIND[entity_type]
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_set():
        proj, item, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
        attributes = _list_settings(client, kind, proj["id"])
        ids = {name: str(_find_setting(attributes, kind, name)["id"]) for name in values}
        if len(set(ids.values())) != len(ids):
            raise ValueError("values names the same attribute twice (names are case-insensitive).")
        current = client.api.get(f"/{segment}/custom-attributes-values/{item['id']}")
        merged = dict(current.get("attributes_values") or {})
        # A cleared value is sent as null, not dropped: Taiga rejects an empty
        # attributes_values ("This field cannot be blank"), which dropping the
        # last set attribute would produce.
        for name, val in values.items():
            merged[ids[name]] = val
        updated = client.api.patch(
            f"/{segment}/custom-attributes-values/{item['id']}",
            json={"attributes_values": merged, "version": current["version"]},
        )
        by_id = {str(a["id"]): a.get("name") for a in attributes}
        return {
            "status": "updated",
            "entity_type": entity_type,
            "ref": ref,
            "values": {
                by_id.get(k, k): v for k, v in (updated.get("attributes_values") or {}).items()
            },
        }

    return core._execute_taiga_operation(
        "set_custom_attribute_values", do_set, f"{entity_type} #{ref} in {project}"
    )


def _project_roles(client, project_id: int) -> List[Dict[str, Any]]:
    return (
        client.api.get("/roles", params={"project": project_id}, headers=_NO_PAGINATION_HEADERS)
        or []
    )


def _member_email(m: Dict[str, Any]) -> str:
    # Established members often have a blank `email`; `user_email` is reliable.
    return (m.get("user_email") or m.get("email") or "").strip().lower()


@app.tool(
    "list_members",
    description=(
        "List a project's members (full name, email, role, is_admin, is_owner, pending for an "
        "invitation not yet accepted) and the project's role names (for invite_members). "
        "project accepts slug or ID."
    ),
)
def list_members(
    project: Union[str, int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_list():
        proj = resolve._resolve_project(client, project)
        members = client.list_resources("memberships", project_id=proj["id"])
        roles = _project_roles(client, proj["id"])
        return {
            "members": [
                {
                    "user_id": m.get("user"),
                    "full_name": m.get("full_name"),
                    "email": m.get("user_email") or m.get("email"),
                    "role": m.get("role_name"),
                    "is_admin": m.get("is_admin", False),
                    "is_owner": m.get("is_owner", False),
                    "pending": m.get("user") is None,
                }
                for m in members
            ],
            "roles": [r.get("name") for r in sorted(roles, key=lambda r: r.get("order") or 0)],
        }

    return core._execute_taiga_operation("list_members", do_list, str(project))


@app.tool(
    "invite_members",
    description=(
        "Invite people to a project by email with one role (a role name from list_members). "
        "Taiga SENDS AN INVITATION EMAIL to each address; message is added to it. Addresses "
        "that are already members are refused before anything is sent. message: at most 255 "
        "characters. Requires PROJECT ADMIN rights. project accepts slug or ID."
    ),
)
def invite_members(
    project: Union[str, int],
    emails: List[str],
    role: str,
    message: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    cleaned = [e.strip() for e in emails or [] if e and e.strip()]
    if not cleaned:
        raise ValueError("emails must contain at least one address.")
    lowered = [e.lower() for e in cleaned]
    if len(set(lowered)) != len(lowered):
        raise ValueError("emails lists the same address twice.")
    if not (role or "").strip():
        raise ValueError("role must not be empty (see list_members for role names).")
    if message and len(message) > 255:
        # Taiga's MembersBulkValidator caps invitation_extra_text at 255; refuse
        # rather than send a message cut off mid-sentence.
        raise ValueError(f"message is {len(message)} characters; Taiga allows at most 255.")
    sid = core._get_session_id(session_id)
    client = core._get_authenticated_client(sid)

    def do_invite():
        proj = resolve._resolve_project(client, project)
        roles = _project_roles(client, proj["id"])
        match = [r for r in roles if (r.get("name") or "").lower() == role.strip().lower()]
        if not match:
            raise ValueError(f"No role named {role!r}. Roles: {[r.get('name') for r in roles]}")
        # Taiga answers a 500 for an address that is already a member (its
        # validator checks a membership `email` that is blank for established
        # members), so refuse those here with a clear message.
        existing = {
            _member_email(m) for m in client.list_resources("memberships", project_id=proj["id"])
        }
        already = [e for e in cleaned if e.lower() in existing]
        if already:
            raise ValueError(f"Already members of '{proj['slug']}': {already}")
        payload: Dict[str, Any] = {
            "project_id": proj["id"],
            # taiga-back _MemberBulkValidator reads `username` (an email or a
            # username) and `role_id`; the pre-v3 full server sent `email`.
            "bulk_memberships": [{"username": e, "role_id": match[0]["id"]} for e in cleaned],
        }
        if message:
            payload["invitation_extra_text"] = message
        with core._admin_required("invite_members", proj["slug"]):
            client.api.post("/memberships/bulk_create", json=payload)
        logger.info(f"Invited {len(cleaned)} address(es) to {proj['slug']} as {match[0]['name']}")
        return {"status": "invited", "emails": cleaned, "role": match[0].get("name")}

    return core._execute_taiga_operation(
        "invite_members", do_invite, f"{len(cleaned)} invite(s) to {project}"
    )
