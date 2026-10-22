"""Attachment tools: list, add (base64 upload), delete — on stories, tasks, issues, epics."""

import base64
import binascii
import logging
import mimetypes
import os
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import _NO_PAGINATION_HEADERS, TaigaNotFoundError

logger = logging.getLogger(__name__)

# Decoded size limit for add_attachment. The file travels base64-encoded inside
# an MCP tool call (≈ 4/3 of its size), which clients and gateways cap well
# below Taiga's own upload limit.
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _attachment_summary(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "size": a.get("size"),
        "description": a.get("description"),
        "url": a.get("url"),
        "is_deprecated": a.get("is_deprecated", False),
        "created_at": a.get("created_date"),
    }


@app.tool(
    "list_attachments",
    description=(
        "List the files attached to a user story, task, issue, or epic: id, name, size (bytes), "
        "description, url, is_deprecated, created_at. The url is a direct download link that "
        "works WITHOUT login — anyone holding it can fetch the file, so don't paste it "
        "anywhere public. entity_type: 'story' (default), 'task', 'issue', or 'epic'. "
        "project accepts slug or ID."
    ),
)
def list_attachments(
    project: Union[str, int],
    ref: int,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_list():
        proj, item, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
        rows = client.api.get(
            f"/{segment}/attachments",
            params={"object_id": item["id"], "project": proj["id"]},
            headers=_NO_PAGINATION_HEADERS,
        )
        return [_attachment_summary(a) for a in rows or []]

    return core._execute_taiga_operation(
        "list_attachments", do_list, f"{entity_type} #{ref} in {project}"
    )


@app.tool(
    "add_attachment",
    description=(
        "Attach a file to a user story, task, issue, or epic. The file is passed inline: "
        "filename (e.g. 'spec.pdf') and content_base64 (the file's bytes, base64-encoded), "
        "up to 10 MB decoded — the server cannot read files from the caller's disk. "
        "entity_type: 'story' (default), 'task', 'issue', or 'epic'. project accepts slug or ID."
    ),
)
def add_attachment(
    project: Union[str, int],
    ref: int,
    filename: str,
    content_base64: str,
    description: Optional[str] = None,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    # Normalise Windows separators too: os.path.basename only splits on "/" here.
    name = os.path.basename((filename or "").strip().replace("\\", "/"))
    if not name:
        raise ValueError("filename must not be empty.")
    # Line-wrapped base64 (`base64` CLI wraps at 76 columns) is still valid input.
    encoded = "".join((content_base64 or "").split())
    # Reject oversize input before decoding it: base64 is ~4/3 of the decoded size.
    if len(encoded) > (_MAX_ATTACHMENT_BYTES + 2) // 3 * 4:
        raise ValueError(f"File exceeds the limit of {_MAX_ATTACHMENT_BYTES} bytes (10 MB).")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content_base64 is not valid base64.")
    if not content:
        raise ValueError("content_base64 decodes to an empty file.")
    if len(content) > _MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"File is {len(content)} bytes; the limit is {_MAX_ATTACHMENT_BYTES} bytes (10 MB)."
        )
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_add():
        proj, item, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
        form = {"project": str(proj["id"]), "object_id": str(item["id"])}
        if description:
            form["description"] = description
        created = client.api.post(
            f"/{segment}/attachments",
            data=form,
            files={"attached_file": (name, content, mime)},
        )
        return {
            "status": "attached",
            "entity_type": entity_type,
            "ref": ref,
            **_attachment_summary(created),
        }

    return core._execute_taiga_operation(
        "add_attachment", do_add, f"{name} on {entity_type} #{ref} in {project}"
    )


@app.tool(
    "delete_attachment",
    description=(
        "PERMANENTLY delete a file attached to a user story, task, issue, or epic (no undo). "
        "attachment_id comes from list_attachments; it must belong to the item given by ref, "
        "otherwise nothing is deleted. entity_type: 'story' (default), 'task', 'issue', or "
        "'epic'. project accepts slug or ID."
    ),
)
def delete_attachment(
    project: Union[str, int],
    ref: int,
    attachment_id: int,
    entity_type: str = "story",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_delete():
        proj, item, segment = resolve._resolve_item_by_ref(client, project, entity_type, ref)
        try:
            attachment = client.api.get(f"/{segment}/attachments/{attachment_id}")
        except TaigaNotFoundError:
            attachment = None
        # An id typo must not delete a file on another item.
        if not attachment or attachment.get("object_id") != item["id"]:
            raise ValueError(
                f"Attachment {attachment_id} is not attached to {entity_type} #{ref} "
                f"(see list_attachments)."
            )
        client.api.delete(f"/{segment}/attachments/{attachment_id}")
        logger.info(
            f"Deleted attachment {attachment_id} ({attachment.get('name')}) "
            f"from {entity_type} #{ref} in {proj['slug']}"
        )
        return {
            "status": "deleted",
            "entity_type": entity_type,
            "ref": ref,
            "attachment_id": attachment_id,
            "name": attachment.get("name"),
        }

    return core._execute_taiga_operation(
        "delete_attachment", do_delete, f"attachment {attachment_id} on {entity_type} #{ref}"
    )
