"""Schema-quality guards on the MCP tool surface (moved from the full-server suite)."""

import asyncio

import src.server_workflow as wf


def _schema_is_typed(prop) -> bool:
    """Whether a JSON-schema fragment carries usable type info.

    A bare `Any` annotation generates an empty `{}` schema (no `type`), and
    `List[Any]` generates a typed array whose `items` are an empty `{}` — both give
    MCP clients no information. A fragment is considered typed when it has a `type`
    (with array `items` recursively typed) or a fully-typed `anyOf`. Object schemas
    are accepted as-is: `Dict[str, Any]` legitimately allows arbitrary values.
    """
    # Non-dict fragments are constraints, not gaps — e.g. a tuple's closed
    # `items: false`. Treat as typed so the recursion can't crash on them.
    if not isinstance(prop, dict):
        return True
    any_of = prop.get("anyOf")
    if any_of:
        return all(_schema_is_typed(sub) for sub in any_of)
    # $ref (nested model), enum/const (Literal) all carry type info without a `type`.
    if any(key in prop for key in ("$ref", "enum", "const")):
        return True
    schema_type = prop.get("type")
    if schema_type is None:
        return False
    if schema_type == "array":
        return _schema_is_typed(prop.get("items", {}))
    return True


def test_tool_params_have_typed_schemas():
    """Every tool parameter must expose a typed JSON schema.

    Guards against reintroducing `Any` (or `List[Any]`) on a tool signature. The
    `kwargs`/`filters` params accept either a JSON object or a JSON string, so they
    resolve to a typed `anyOf`, never `{}`.
    """
    tools = asyncio.run(wf.mcp.list_tools())
    assert tools, "expected the server to expose tools"
    offenders = [
        f"{tool.name}.{name}: {prop}"
        for tool in tools
        for name, prop in (tool.inputSchema or {}).get("properties", {}).items()
        if name != "session_id" and not _schema_is_typed(prop)
    ]
    assert not offenders, "untyped (Any) tool params found:\n" + "\n".join(offenders)
