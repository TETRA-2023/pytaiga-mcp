"""Unit tests for the MCP server (src/server/): name resolution, tools, offload."""

import asyncio
import sys
import uuid
from unittest.mock import DEFAULT, MagicMock, call

import pytest

import src.server as wf
from src import taiga_client
from src.server import core
from src.taiga_client import (
    TaigaAPIError,
    TaigaAuthenticationError,
    TaigaBadRequestError,
    TaigaConcurrencyError,
    TaigaException,
    TaigaNotFoundError,
    TaigaServerError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_sessions():
    """Ensure active_sessions and cache are empty before/after each test."""
    wf.active_sessions.clear()
    wf._session_cache.clear()
    yield
    wf.active_sessions.clear()
    wf._session_cache.clear()


@pytest.fixture
def session(mock_client):
    session_id = str(uuid.uuid4())
    wf.active_sessions[session_id] = mock_client
    wf.active_sessions[wf.DEFAULT_SESSION_ID] = mock_client
    return session_id


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.is_authenticated = True
    return client


class Seq:
    """Successive answers for one routed path (e.g. several POST /tasks)."""

    def __init__(self, *values):
        self._values = iter(values)

    def next(self):
        return next(self._values)


def route(mock_client, get=None, post=None, patch=None, delete=None):
    """Answer ``mock_client.api.<verb>(path, ...)`` by exact path.

    Each value is returned as-is, raised if it is an exception, called with the
    request kwargs (``params=``/``json=``) if callable, or consumed in order if
    it is a ``Seq``. Unrouted GETs fall back to ``api.get.return_value`` (the
    project-resolution mock most tests set). For a write verb that is routed,
    any unrouted path fails the test, so a test cannot pass without hitting
    the endpoint it asserts on.
    """
    for verb, table in (("get", get), ("post", post), ("patch", patch), ("delete", delete)):
        if table is None:
            continue

        def side_effect(path, *args, _verb=verb, _table=table, **kwargs):
            if path not in _table:
                if _verb == "get":
                    return DEFAULT
                raise AssertionError(f"unrouted {_verb.upper()} {path}")
            value = _table[path]
            if isinstance(value, Seq):
                value = value.next()
            if isinstance(value, BaseException):
                raise value
            return value(**kwargs) if callable(value) else value

        getattr(mock_client.api, verb).side_effect = side_effect


def sent(mock_client, verb, path):
    """The ``json=`` payloads sent with ``verb`` to ``path``, in call order."""
    return [
        c.kwargs.get("json")
        for c in getattr(mock_client.api, verb).call_args_list
        if c.args and c.args[0] == path
    ]


# ---------------------------------------------------------------------------
# Tool input-schema typing (regression for empty `Any` schemas)
# ---------------------------------------------------------------------------


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


def test_schema_is_typed_detects_gaps():
    """The guard must reject empty and `List[Any]`-style item gaps, accept real types."""
    # Untyped — must be rejected.
    assert not _schema_is_typed({})  # Any
    assert not _schema_is_typed({"type": "array", "items": {}})  # List[Any]
    assert not _schema_is_typed({"anyOf": [{"type": "string"}, {}]})  # Union[str, Any]
    # Typed — must be accepted.
    assert _schema_is_typed({"type": "string"})
    assert _schema_is_typed({"anyOf": [{"type": "string"}, {"type": "integer"}]})
    assert _schema_is_typed({"type": "array", "items": {"type": "string"}})
    assert _schema_is_typed({"type": "object", "additionalProperties": True})  # Dict[str, Any]
    # Shapes without a top-level `type` that still carry info — must be accepted,
    # and must not crash the recursion.
    assert _schema_is_typed({"$ref": "#/$defs/Foo"})  # nested model
    assert _schema_is_typed({"enum": ["a", "b"]})  # Literal
    assert _schema_is_typed({"const": "x"})
    assert _schema_is_typed({"type": "array", "prefixItems": [{"type": "string"}], "items": False})


def test_tool_params_have_typed_schemas():
    """Every tool parameter must expose a typed JSON schema.

    Guards against reintroducing `Any` (or `List[Any]`) on a tool signature, which
    would strip type information clients rely on to populate args — especially for
    required params like `project`.
    """
    tools = asyncio.run(wf.mcp.list_tools())
    assert tools, "expected the workflow server to expose tools"
    offenders = [
        f"{tool.name}.{name}: {prop}"
        for tool in tools
        for name, prop in (tool.inputSchema or {}).get("properties", {}).items()
        if name != "session_id" and not _schema_is_typed(prop)
    ]
    assert not offenders, "untyped (Any) tool params found:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


class TestSessionHelpers:
    def test_get_session_id_explicit(self, mock_client):
        sid = str(uuid.uuid4())
        wf.active_sessions[sid] = mock_client
        assert wf._get_session_id(sid) == sid

    def test_get_session_id_defaults_to_default(self, mock_client):
        wf.active_sessions[wf.DEFAULT_SESSION_ID] = mock_client
        assert wf._get_session_id() == wf.DEFAULT_SESSION_ID

    def test_get_session_id_raises_when_no_default(self):
        with pytest.raises(ValueError, match="No session_id provided"):
            wf._get_session_id()

    def test_get_session_id_reports_failed_auto_login(self, monkeypatch):
        # #197: the startup login failure is the cause, not a missing session_id.
        monkeypatch.setattr(core, "auto_login_error", "TaigaNotFoundError: Not found.")
        with pytest.raises(ValueError, match="Auto-login at startup failed: TaigaNotFoundError"):
            wf._get_session_id()

    def test_get_authenticated_client_ok(self, session, mock_client):
        assert wf._get_authenticated_client(session) is mock_client

    def test_get_authenticated_client_missing_raises(self):
        with pytest.raises(PermissionError):
            wf._get_authenticated_client("nonexistent")


# ---------------------------------------------------------------------------
# Session cache helpers
# ---------------------------------------------------------------------------


class TestSessionCache:
    def test_cache_created_on_first_access(self):
        sid = "test-session"
        cache = wf._session_cache_for(sid)
        assert isinstance(cache, dict)
        assert sid in wf._session_cache

    def test_cache_cleared_on_logout(self, session, mock_client):
        wf._session_cache_for(session)["key"] = "value"
        wf._clear_session_cache(session)
        assert session not in wf._session_cache

    def test_logout_clears_cache(self, session):
        wf._session_cache_for(session)["key"] = "value"
        wf.logout(session_id=session)
        assert session not in wf._session_cache


# ---------------------------------------------------------------------------
# Name resolution: _resolve_project
# ---------------------------------------------------------------------------


class TestResolveProject:
    def test_resolves_by_slug(self, mock_client):
        mock_client.api.get.return_value = {"id": 1, "name": "Test", "slug": "test"}
        result = wf._resolve_project(mock_client, "test")
        mock_client.api.get.assert_called_once_with("/projects/by_slug", params={"slug": "test"})
        assert result["id"] == 1

    def test_resolves_by_int_id(self, mock_client):
        mock_client.api.get.return_value = {"id": 42, "name": "Test", "slug": "test"}
        result = wf._resolve_project(mock_client, 42)
        mock_client.api.get.assert_called_once_with("/projects/42")
        assert result["id"] == 42

    def test_resolves_numeric_string_as_id(self, mock_client):
        mock_client.api.get.return_value = {"id": 7, "name": "Test", "slug": "test"}
        wf._resolve_project(mock_client, "7")
        mock_client.api.get.assert_called_once_with("/projects/7")

    def test_raises_when_not_found(self, mock_client):
        mock_client.api.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            wf._resolve_project(mock_client, "missing")


# ---------------------------------------------------------------------------
# Name resolution: _resolve_sprint
# ---------------------------------------------------------------------------


class TestResolveSprint:
    def _make_client(self, milestones):
        client = MagicMock()
        client.list_resources.return_value = milestones
        return client

    def test_resolves_by_name(self):
        client = self._make_client([{"id": 1, "name": "Sprint 1", "closed": False}])
        result = wf._resolve_sprint(client, 99, "Sprint 1")
        assert result["id"] == 1

    def test_resolves_by_int_id(self):
        client = self._make_client([{"id": 5, "name": "Sprint 5", "closed": False}])
        result = wf._resolve_sprint(client, 99, 5)
        assert result["id"] == 5

    def test_digit_name_wins_over_id(self):
        # A sprint named "42" must not resolve to the other sprint whose id is 42.
        client = self._make_client(
            [{"id": 42, "name": "Sprint 3", "closed": False}, {"id": 7, "name": "42"}]
        )
        assert wf._resolve_sprint(client, 99, "42")["id"] == 7
        assert wf._resolve_sprint(client, 99, "Sprint 3")["id"] == 42
        assert wf._resolve_sprint(client, 99, 42)["id"] == 42

    def test_bool_rejected(self):
        client = self._make_client([{"id": 1, "name": "Sprint 1", "closed": False}])
        with pytest.raises(ValueError, match="name or an ID"):
            wf._resolve_sprint(client, 99, True)

    def test_raises_when_sprint_not_found(self):
        client = self._make_client([{"id": 1, "name": "Sprint 1", "closed": False}])
        with pytest.raises(ValueError, match="not found"):
            wf._resolve_sprint(client, 99, "Sprint 99")

    def test_current_sprint_single_match(self):
        milestones = [
            {
                "id": 3,
                "name": "Active Sprint",
                "closed": False,
                "estimated_start": "2000-01-01",
                "estimated_finish": "2099-12-31",
            }
        ]
        client = self._make_client(milestones)
        result = wf._resolve_sprint(client, 99, None)
        assert result["id"] == 3

    def test_current_sprint_none_raises(self):
        milestones = [
            {
                "id": 1,
                "name": "Old Sprint",
                "closed": False,
                "estimated_start": "2000-01-01",
                "estimated_finish": "2000-01-15",
            }
        ]
        client = self._make_client(milestones)
        with pytest.raises(ValueError, match="No current sprint"):
            wf._resolve_sprint(client, 99, None)

    def test_current_sprint_multiple_raises(self):
        milestones = [
            {
                "id": 1,
                "name": "Sprint A",
                "closed": False,
                "estimated_start": "2000-01-01",
                "estimated_finish": "2099-12-31",
            },
            {
                "id": 2,
                "name": "Sprint B",
                "closed": False,
                "estimated_start": "2000-01-01",
                "estimated_finish": "2099-12-31",
            },
        ]
        client = self._make_client(milestones)
        with pytest.raises(ValueError, match="Multiple active sprints"):
            wf._resolve_sprint(client, 99, None)


# ---------------------------------------------------------------------------
# Name resolution: _resolve_status
# ---------------------------------------------------------------------------


class TestResolveStatus:
    def test_resolves_status_by_name(self, mock_client):
        mock_client.list_resources.return_value = [
            {"id": 10, "name": "In Progress"},
            {"id": 11, "name": "Done"},
        ]
        result = wf._resolve_status(mock_client, 1, "story", "In Progress", "sess")
        assert result == 10

    def test_case_insensitive(self, mock_client):
        mock_client.list_resources.return_value = [{"id": 20, "name": "Done"}]
        assert wf._resolve_status(mock_client, 1, "story", "done", "sess") == 20

    def test_cached_on_second_call(self, mock_client):
        mock_client.list_resources.return_value = [{"id": 5, "name": "New"}]
        wf._resolve_status(mock_client, 1, "story", "New", "sess")
        wf._resolve_status(mock_client, 1, "story", "New", "sess")
        # list_resources called only once
        mock_client.list_resources.assert_called_once()

    def test_raises_when_not_found(self, mock_client):
        mock_client.list_resources.return_value = [{"id": 1, "name": "New"}]
        with pytest.raises(ValueError, match="not found"):
            wf._resolve_status(mock_client, 1, "story", "Nonexistent", "sess")

    def test_raises_for_unknown_entity_type(self, mock_client):
        with pytest.raises(ValueError, match="Cannot resolve statuses"):
            wf._resolve_status(mock_client, 1, "wiki", "whatever", "sess")


# ---------------------------------------------------------------------------
# Name resolution: _resolve_user
# ---------------------------------------------------------------------------


class TestResolveUser:
    def _members(self):
        """A REAL /memberships payload: no user_extra_info, no username, and the
        flat `email` blank while `user_email` carries the address."""
        return [
            {
                "user": 42,
                "full_name": "Alice Martin",
                "email": "",
                "user_email": "alice@example.com",
            }
        ]

    def test_resolves_by_user_email(self, mock_client):
        # Memberships expose no username, so email is the practical handle.
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, "alice@example.com", "sess") == 42

    def test_resolves_by_full_name(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, "Alice Martin", "sess") == 42

    def test_resolves_by_username_only_on_non_memberships_payload(self, mock_client):
        # Forward-compat: resolution still probes user_extra_info for endpoints
        # that return it. A memberships payload never does, so this shape is
        # deliberately synthetic and must not be copied into other fixtures.
        mock_client.list_resources.return_value = [
            {"user": 7, "user_extra_info": {"username": "carol"}}
        ]
        assert wf._resolve_user(mock_client, 1, "CAROL", "sess") == 7

    def test_raises_when_not_found(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        with pytest.raises(ValueError, match="not found"):
            wf._resolve_user(mock_client, 1, "bob", "sess")

    def test_resolves_past_member_with_null_fields(self, mock_client):
        # Regression for pytaiga-mcp#120: a pending-invite membership with null
        # email/full_name must not abort resolution before the valid member is
        # reached (previously raised 'NoneType' ... 'lower').
        members = [
            {"user": None, "email": None, "user_email": None, "full_name": None},
            *self._members(),
        ]
        mock_client.list_resources.return_value = members
        assert wf._resolve_user(mock_client, 1, "alice@example.com", "sess") == 42

    def test_int_identifier_passthrough(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, 99, "sess") == 99


# ---------------------------------------------------------------------------
# Tool smoke tests
# ---------------------------------------------------------------------------


class TestListProjects:
    def test_returns_project_list(self, session, mock_client):
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "Proj A", "slug": "proj-a", "description": None, "is_private": False}
        ]
        result = wf.list_projects(session_id=session)
        assert len(result) == 1
        assert result[0]["slug"] == "proj-a"


class TestCreateStory:
    def test_creates_story_minimal(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "test", "name": "Test"}
        route(mock_client, post={"/userstories": {"id": 100, "ref": 5, "subject": "As a user"}})
        result = wf.create_story("test", "As a user", session_id=session)
        assert result["status"] == "created"
        assert result["ref"] == 5
        # No milestone on the created story -> no sprint, and notably not a crash
        # from reaching into a phantom milestone_extra_info.
        assert result["sprint"] is None

    def test_returned_sprint_comes_from_flat_milestone_name(self, session, mock_client):
        # create_story's own return block had the same phantom-key bug as
        # _story_summary; cover it directly rather than only via the helper.
        mock_client.api.get.return_value = {"id": 1, "slug": "test", "name": "Test"}
        route(
            mock_client,
            post={
                "/userstories": {
                    "id": 101,
                    "ref": 6,
                    "subject": "Sprinted",
                    "milestone": 85,
                    "milestone_name": "Sprint 9",
                }
            },
        )
        result = wf.create_story("test", "Sprinted", session_id=session)
        assert result["sprint"] == "Sprint 9"

    def test_creates_story_with_assignee(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "test", "name": "Test"}
        mock_client.list_resources.return_value = [
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@example.com",
            }
        ]
        route(mock_client, post={"/userstories": {"id": 200, "ref": 10, "subject": "Story"}})
        result = wf.create_story("test", "Story", assignee="bob", session_id=session)
        assert result["status"] == "created"
        (payload,) = sent(mock_client, "post", "/userstories")
        assert payload == {"project": 1, "subject": "Story", "assigned_to": 7}

    def test_creates_story_with_epic_link(self, session, mock_client):
        # api.get: 1st resolves the project (by_slug), 2nd resolves the epic (by_ref).
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "test", "name": "Test"},
            {"id": 99, "ref": 7, "subject": "Epic"},
        ]
        route(
            mock_client,
            post={
                "/userstories": {"id": 100, "ref": 5, "subject": "Story"},
                "/epics/99/related_userstories": {},
            },
        )
        result = wf.create_story("test", "Story", epic=7, session_id=session)
        assert result["status"] == "created"
        assert result["epic_linked"] is True
        # Taiga's related_userstories endpoint requires 'epic' + 'user_story'
        # (not project_id/user_story_id) — same payload as update_story relink.
        assert sent(mock_client, "post", "/epics/99/related_userstories") == [
            {"epic": 99, "user_story": 100}
        ]

    def test_epic_not_found_creates_no_story(self, session, mock_client):
        # Epic is validated before creation, so a bad ref must NOT leave an
        # orphaned (created-but-unlinked) story behind.
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "test", "name": "Test"},
            None,  # /epics/by_ref → not found
        ]
        with pytest.raises(ValueError, match="Epic #7 not found"):
            wf.create_story("test", "Story", epic=7, session_id=session)
        mock_client.api.post.assert_not_called()


class TestSetStoryStatus:
    def test_resolves_status_and_updates(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [{"id": 99, "name": "Done"}]
        route(
            mock_client,
            get={"/userstories/by_ref": {"id": 50, "ref": 3, "version": 1}},
            patch={
                "/userstories/50": {
                    "ref": 3,
                    "status_extra_info": {"name": "Done"},
                    "is_closed": True,
                }
            },
        )
        result = wf.set_story_status("p", 3, "Done", session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True
        assert sent(mock_client, "patch", "/userstories/50") == [{"status": 99, "version": 1}]


class TestGetSprintBoard:
    def test_returns_board_with_tasks(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.side_effect = [
            # milestones
            [
                {
                    "id": 10,
                    "name": "Sprint 1",
                    "closed": False,
                    "estimated_start": "2000-01-01",
                    "estimated_finish": "2099-12-31",
                }
            ],
            # user_stories
            [
                {
                    "id": 50,
                    "ref": 1,
                    "subject": "Story A",
                    "status_extra_info": {"name": "In Progress"},
                    "assigned_to_extra_info": {"full_name_display": "Alice"},
                    "milestone_name": "Sprint 1",
                    "is_blocked": False,
                    "is_closed": False,
                    "tags": [],
                }
            ],
            # tasks
            [
                {
                    "id": 200,
                    "ref": 5,
                    "subject": "Task 1",
                    "user_story": 50,
                    "status_extra_info": {"name": "Done"},
                    "assigned_to_extra_info": {"full_name_display": "Alice"},
                    "is_blocked": False,
                }
            ],
        ]
        result = wf.get_sprint_board("p", session_id=session)
        assert result["sprint"]["name"] == "Sprint 1"
        assert result["summary"]["total_stories"] == 1
        assert len(result["stories"]) == 1
        assert len(result["stories"][0]["tasks"]) == 1


def _api_error(exc_cls, status_code: int, message: str = "boom"):
    """Build a Taiga API exception as src.taiga_client raises it for ``status_code``."""
    return exc_cls(status_code, message, {"_error_message": message})


class TestUpsertWiki:
    def test_creates_new_page_when_not_found(self, session, mock_client):
        # REAL behaviour: /wiki/by_slug RAISES 404 for an unknown slug, it does not
        # return None. The previous fixture returned None, so this test passed while
        # the create path was unreachable in production — the 404 escaped before the
        # else branch. Modelling the raise is what makes this a real regression test.
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},  # _resolve_project
            _api_error(TaigaNotFoundError, 404, "wiki page not found"),  # /wiki/by_slug
        ]
        route(mock_client, post={"/wiki": {"id": 9, "slug": "my-page"}})
        result = wf.upsert_wiki("p", "my-page", "Content here", session_id=session)
        assert result["status"] == "created"
        assert sent(mock_client, "post", "/wiki") == [
            {"project": 1, "slug": "my-page", "content": "Content here"}
        ]

    def test_non_404_lookup_error_propagates(self, session, mock_client):
        # The guard must be narrow: a server or auth failure on the lookup is not
        # "page does not exist" and must not silently create a duplicate page.
        # Asserting the exact type matters — _execute_taiga_operation re-raises
        # TaigaAPIError unchanged, so a bare `raises(Exception)` would also pass if the
        # call blew up for an unrelated reason, which is not what this test claims.
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            _api_error(TaigaServerError, 500, "upstream exploded"),
        ]
        with pytest.raises(TaigaServerError):
            wf.upsert_wiki("p", "my-page", "Content here", session_id=session)
        mock_client.api.post.assert_not_called()

    def test_updates_existing_page(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            {"id": 9, "slug": "my-page", "version": 3},
        ]
        route(mock_client, patch={"/wiki/9": {"id": 9, "slug": "my-page"}})
        result = wf.upsert_wiki("p", "my-page", "Updated content", session_id=session)
        assert result["status"] == "updated"
        assert sent(mock_client, "patch", "/wiki/9") == [
            {"version": 3, "slug": "my-page", "content": "Updated content"}
        ]


class TestUpdateIssue:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _issue(self):
        return {"id": 300, "ref": 5, "subject": "Bug", "version": 2}

    def _route(self, mock_client, updated):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={"/issues/by_ref": self._issue()},
            patch={"/issues/300": updated},
        )

    def test_resolves_status_by_name(self, session, mock_client):
        self._route(
            mock_client,
            {
                "ref": 5,
                "subject": "Bug",
                "status_extra_info": {"name": "In Progress"},
                "assigned_to_extra_info": None,
                "is_blocked": False,
            },
        )
        mock_client.list_resources.return_value = [{"id": 20, "name": "In Progress"}]
        wf.update_issue("p", 5, status="In Progress", session_id=session)
        assert sent(mock_client, "patch", "/issues/300") == [{"version": 2, "status": 20}]

    def test_resolves_priority_by_name(self, session, mock_client):
        self._route(
            mock_client,
            {
                "ref": 5,
                "subject": "Bug",
                "status_extra_info": None,
                "priority": 3,
                "assigned_to_extra_info": None,
                "is_blocked": False,
            },
        )
        mock_client.list_resources.return_value = [{"id": 3, "name": "High"}]
        wf.update_issue("p", 5, priority="High", session_id=session)
        assert sent(mock_client, "patch", "/issues/300") == [{"version": 2, "priority": 3}]

    def test_no_op_raises(self, session, mock_client):
        self._route(mock_client, {})
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_issue("p", 5, session_id=session)
        mock_client.api.patch.assert_not_called()

    def test_version_sent_once_from_the_fetched_issue(self, session, mock_client):
        # Pre-v3 this guarded pytaigaclient's edit(version=, data=) split; with the
        # raw PATCH the contract is simply: one version key, the fetched one.
        self._route(
            mock_client,
            {
                "ref": 5,
                "subject": "Bug",
                "status_extra_info": {"name": "Done"},
                "assigned_to_extra_info": None,
                "is_blocked": False,
            },
        )
        mock_client.list_resources.return_value = [{"id": 20, "name": "Done"}]
        wf.update_issue("p", 5, status="Done", session_id=session)
        (payload,) = sent(mock_client, "patch", "/issues/300")
        assert payload["version"] == 2


# ---------------------------------------------------------------------------
# Bug fixes from PR #72 review
# ---------------------------------------------------------------------------


class TestSessionStatusParity:
    def test_returns_session_id_when_token_invalid(self, session, mock_client):
        route(
            mock_client,
            get={"/users/me": TaigaAuthenticationError(401, "Token is invalid")},
        )
        result = wf.session_status(session_id=session)
        assert result["status"] == "inactive"
        assert result["reason"] == "token_invalid"
        assert result["session_id"] == session
        assert session not in wf.active_sessions

    def test_token_invalid_clears_session_cache(self, session, mock_client):
        # Like logout(): a dropped session must not leave its lookup cache behind
        # for the next login() that reuses the same id (e.g. "default").
        wf._session_cache_for(session)["members_1"] = ["stale"]
        route(
            mock_client,
            get={"/users/me": TaigaAuthenticationError(401, "Token is invalid")},
        )
        wf.session_status(session_id=session)
        assert session not in wf._session_cache

    def test_transient_error_keeps_session(self, session, mock_client):
        # A timeout or 5xx says nothing about the token: keep the session.
        route(mock_client, get={"/users/me": TaigaException("Request failed: ReadTimeout")})
        with pytest.raises(TaigaException):
            wf.session_status(session_id=session)
        assert wf.active_sessions[session] is mock_client

    def test_logout_closes_only_unaliased_wrappers(self, session, mock_client):
        # login() stores one wrapper under its UUID and "default": logging out
        # the UUID must not close the pool the default alias still uses.
        wf.logout(session_id=session)
        mock_client.close.assert_not_called()
        wf.logout(session_id=wf.DEFAULT_SESSION_ID)
        mock_client.close.assert_called_once()

    def test_returns_not_authenticated_when_client_unauthed(self):
        client = MagicMock()
        client.is_authenticated = False
        sid = str(uuid.uuid4())
        wf.active_sessions[sid] = client
        result = wf.session_status(session_id=sid)
        assert result["status"] == "inactive"
        assert result["reason"] == "not_authenticated"
        assert result["session_id"] == sid


class TestResolveStoryRefs:
    def test_batches_to_single_list_call(self, mock_client):
        mock_client.list_resources.return_value = [
            {"id": 100, "ref": 1},
            {"id": 200, "ref": 2},
            {"id": 300, "ref": 3},
        ]
        ids = wf._resolve_story_refs(mock_client, 99, [3, 1, 2])
        assert ids == [300, 100, 200]
        mock_client.list_resources.assert_called_once_with("user_stories", project_id=99)

    def test_empty_input_no_api_call(self, mock_client):
        assert wf._resolve_story_refs(mock_client, 99, []) == []
        mock_client.list_resources.assert_not_called()

    def test_missing_refs_listed_in_error(self, mock_client):
        mock_client.list_resources.return_value = [{"id": 100, "ref": 1}]
        with pytest.raises(ValueError, match=r"\[2, 5\]"):
            wf._resolve_story_refs(mock_client, 99, [1, 2, 5])


class TestCreateIssueNoneDefaultsGuard:
    def test_omits_priority_severity_type_when_project_has_none(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        # Project with no priorities/severities/types configured.
        def list_resources_stub(resource, project_id=None, **_kwargs):
            return {
                "priorities": [],
                "severities": [],
                "issue_types": [],
                "issue_statuses": [{"id": 7, "name": "New"}],
            }[resource]

        mock_client.list_resources.side_effect = list_resources_stub
        route(mock_client, post={"/issues": {"id": 1, "ref": 9, "subject": "Bug"}})

        wf.create_issue("p", "Bug", session_id=session)
        # No None values sent for the unconfigured attributes; default status wired up.
        assert sent(mock_client, "post", "/issues") == [
            {"project": 1, "subject": "Bug", "status": 7}
        ]


class TestGetEpicOverviewBatched:
    def test_single_list_call_no_per_story_fetch(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},  # _resolve_project
            {"id": 50, "ref": 4, "subject": "E"},  # /epics/by_ref
        ]
        mock_client.list_resources.return_value = [
            {
                "id": 100,
                "ref": 1,
                "subject": "Story A",
                "status_extra_info": {"name": "Done"},
                "assigned_to_extra_info": None,
                "is_blocked": False,
                "is_closed": True,
                "tags": [],
            },
            {
                "id": 101,
                "ref": 2,
                "subject": "Story B",
                "status_extra_info": {"name": "Done"},
                "assigned_to_extra_info": None,
                "is_blocked": False,
                "is_closed": True,
                "tags": [],
            },
        ]
        result = wf.get_epic_overview("p", 4, session_id=session)
        assert result["summary"]["total_stories"] == 2
        assert result["summary"]["stories_by_status"] == {"Done": 2}
        # One list call, zero per-story user_stories.get() calls.
        mock_client.list_resources.assert_called_once_with("user_stories", project_id=1, epic=50)
        assert [c.args[0] for c in mock_client.api.get.call_args_list] == [
            "/projects/by_slug",
            "/epics/by_ref",
        ]


class TestUpdateStoryEpicRelink:
    """Regression tests for the update_story epic= parameter (round-2 review)."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _story_with_epics(self, epic_dicts):
        return {
            "id": 100,
            "ref": 5,
            "subject": "Story",
            "version": 1,
            "epics": epic_dicts,
        }

    def _route(self, mock_client, story, epic=None):
        route(
            mock_client,
            get={
                "/projects/by_slug": self._project(),
                "/userstories/by_ref": story,
                "/epics/by_ref": epic,
            },
        )

    def test_epic_zero_unlinks_all_no_post(self, session, mock_client):
        # Story currently linked to two epics.
        self._route(
            mock_client, self._story_with_epics([{"id": 50, "ref": 1}, {"id": 51, "ref": 2}])
        )

        wf.update_story("p", 5, epic=0, session_id=session)

        # Two DELETEs (extracted from dict[id]), no POST, no new-epic lookup.
        assert mock_client.api.delete.call_count == 2
        mock_client.api.delete.assert_any_call("/epics/50/related_userstories/100")
        mock_client.api.delete.assert_any_call("/epics/51/related_userstories/100")
        mock_client.api.post.assert_not_called()

    def test_epic_relink_lookup_before_delete(self, session, mock_client):
        self._route(
            mock_client,
            self._story_with_epics([{"id": 50, "ref": 1}]),
            epic={"id": 99, "ref": 7, "subject": "New Epic"},
        )

        wf.update_story("p", 5, epic=7, session_id=session)

        # The new epic is looked up before anything is unlinked.
        order = [(c[0], c.args[0]) for c in mock_client.api.mock_calls if c[0] in ("get", "delete")]
        assert order.index(("get", "/epics/by_ref")) < order.index(
            ("delete", "/epics/50/related_userstories/100")
        )
        # Old link removed (one DELETE), new link created (one POST).
        mock_client.api.delete.assert_called_once_with("/epics/50/related_userstories/100")
        mock_client.api.post.assert_called_once_with(
            "/epics/99/related_userstories",
            json={"epic": 99, "user_story": 100},
        )

    def test_epic_not_found_does_not_unlink(self, session, mock_client):
        self._route(mock_client, self._story_with_epics([{"id": 50, "ref": 1}]), epic=None)

        with pytest.raises(ValueError, match="Epic #7 not found"):
            wf.update_story("p", 5, epic=7, session_id=session)

        # Critical: validation runs BEFORE any mutation. The story's existing
        # link to epic #50 must be intact when the call fails.
        mock_client.api.delete.assert_not_called()
        mock_client.api.post.assert_not_called()

    def test_epic_link_when_story_has_none(self, session, mock_client):
        self._route(
            mock_client,
            self._story_with_epics([]),
            epic={"id": 99, "ref": 7, "subject": "New Epic"},
        )

        wf.update_story("p", 5, epic=7, session_id=session)

        mock_client.api.delete.assert_not_called()
        mock_client.api.post.assert_called_once_with(
            "/epics/99/related_userstories",
            json={"epic": 99, "user_story": 100},
        )

    def test_epic_zero_reports_both_replaced_links(self, session, mock_client):
        # #204: a story in two epics. epic=0 drops both, and the result says so.
        self._route(
            mock_client,
            self._story_with_epics(
                [{"id": 50, "ref": 1, "subject": "A"}, {"id": 51, "ref": 2, "subject": "B"}]
            ),
        )
        result = wf.update_story("p", 5, epic=0, session_id=session)
        assert result["epics_before"] == [
            {"ref": 1, "subject": "A"},
            {"ref": 2, "subject": "B"},
        ]
        assert result["epics"] == []

    def test_epic_relink_reports_new_link_not_stale_read(self, session, mock_client):
        # The story read before the relink still lists the old epic; the result
        # must show the link as it is after the relink.
        self._route(
            mock_client,
            self._story_with_epics([{"id": 50, "ref": 1, "subject": "Old"}]),
            epic={"id": 99, "ref": 7, "subject": "New Epic"},
        )
        result = wf.update_story("p", 5, epic=7, session_id=session)
        assert result["epics_before"] == [{"ref": 1, "subject": "Old"}]
        assert result["epics"] == [{"ref": 7, "subject": "New Epic"}]

    def test_no_epic_arg_reports_no_epics_before(self, session, mock_client):
        self._route(mock_client, self._story_with_epics([{"id": 50, "ref": 1, "subject": "A"}]))
        route(mock_client, patch={"/userstories/100": lambda json: {**json, "ref": 5}})
        result = wf.update_story("p", 5, subject="New", session_id=session)
        assert "epics_before" not in result
        mock_client.api.delete.assert_not_called()

    def test_description_says_epic_replaces_links(self):
        description = wf.mcp._tool_manager.get_tool("update_story").description
        assert "REPLACES" in description
        assert "link_to_epic" in description

    def test_no_op_when_no_fields_and_no_epic(self, session, mock_client):
        self._route(mock_client, self._story_with_epics([]))

        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_story("p", 5, session_id=session)


# ---------------------------------------------------------------------------
# Task tools (v2.1 — task/intent surface)
# ---------------------------------------------------------------------------


class TestCreateTask:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _route(self, mock_client, story, created=None):
        mock_client.api.get.return_value = self._project()
        route(mock_client, get={"/userstories/by_ref": story}, post={"/tasks": created or {}})

    def test_creates_task_minimal(self, session, mock_client):
        self._route(
            mock_client, {"id": 50, "ref": 42}, {"id": 200, "ref": 7, "subject": "Implement API"}
        )
        result = wf.create_task("p", 42, "Implement API", session_id=session)
        assert result["status"] == "created"
        assert result["ref"] == 7
        assert result["parent_story_ref"] == 42
        assert sent(mock_client, "post", "/tasks") == [
            {"project": 1, "subject": "Implement API", "user_story": 50}
        ]

    def test_creates_task_with_assignee_and_status(self, session, mock_client):
        self._route(mock_client, {"id": 50, "ref": 42}, {"id": 200, "ref": 7, "subject": "API"})
        # Two list_resources calls — one for task_statuses, one for memberships.
        mock_client.list_resources.side_effect = [
            [{"id": 11, "name": "In Progress"}],
            [
                {
                    "user": 7,
                    "full_name": "Alice",
                    "email": "alice@example.com",
                }
            ],
        ]
        wf.create_task("p", 42, "API", status="In Progress", assignee="alice", session_id=session)
        (payload,) = sent(mock_client, "post", "/tasks")
        assert payload["status"] == 11
        assert payload["assigned_to"] == 7

    def test_raises_when_parent_story_missing(self, session, mock_client):
        self._route(mock_client, None)
        with pytest.raises(ValueError, match=r"User story #42 not found"):
            wf.create_task("p", 42, "T", session_id=session)
        mock_client.api.post.assert_not_called()


class TestUpdateTask:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _task(self):
        return {"id": 200, "ref": 7, "subject": "T", "version": 3, "user_story": 50}

    def _route(self, mock_client, updated=None, story=None):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={"/tasks/by_ref": self._task(), "/userstories/by_ref": story},
            patch={
                "/tasks/200": updated
                or {
                    "ref": 7,
                    "subject": "T",
                    "status_extra_info": None,
                    "assigned_to_extra_info": None,
                    "is_blocked": False,
                }
            },
        )

    def test_resolves_status_by_name(self, session, mock_client):
        self._route(
            mock_client,
            {
                "ref": 7,
                "subject": "T",
                "status_extra_info": {"name": "Done"},
                "assigned_to_extra_info": None,
                "is_blocked": False,
            },
        )
        mock_client.list_resources.return_value = [{"id": 12, "name": "Done"}]
        wf.update_task("p", 7, status="Done", session_id=session)
        assert sent(mock_client, "patch", "/tasks/200") == [{"version": 3, "status": 12}]

    def test_reparent_resolves_story_ref(self, session, mock_client):
        self._route(mock_client, story={"id": 99, "ref": 44})
        wf.update_task("p", 7, story_ref=44, session_id=session)
        assert sent(mock_client, "patch", "/tasks/200") == [{"version": 3, "user_story": 99}]

    def test_reparent_result_shows_new_story_ref(self, session, mock_client):
        self._route(
            mock_client,
            updated={"ref": 7, "user_story": 99, "user_story_extra_info": {"id": 99, "ref": 44}},
            story={"id": 99, "ref": 44},
        )
        result = wf.update_task("p", 7, story_ref=44, session_id=session)
        assert result["story_ref"] == 44

    def test_sprint_move_supported(self, session, mock_client):
        self._route(mock_client)
        mock_client.list_resources.return_value = [{"id": 33, "name": "Sprint 2", "closed": False}]
        wf.update_task("p", 7, sprint="Sprint 2", session_id=session)
        assert sent(mock_client, "patch", "/tasks/200") == [{"version": 3, "milestone": 33}]

    def test_no_op_raises(self, session, mock_client):
        self._route(mock_client)
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_task("p", 7, session_id=session)
        mock_client.api.patch.assert_not_called()


class TestSetTaskStatus:
    def test_resolves_and_updates(self, session, mock_client):
        project = {"id": 1, "slug": "p", "name": "P"}
        task = {"id": 200, "ref": 7, "version": 2}

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return project

        mock_client.api.get.side_effect = api_get
        mock_client.list_resources.return_value = [{"id": 99, "name": "Done"}]
        route(
            mock_client,
            patch={
                "/tasks/200": {"ref": 7, "status_extra_info": {"name": "Done"}, "is_closed": True}
            },
        )
        result = wf.set_task_status("p", 7, "Done", session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True
        assert sent(mock_client, "patch", "/tasks/200") == [{"version": 2, "status": 99}]
        # Confirm uses task_statuses, not userstory_statuses.
        mock_client.list_resources.assert_called_once_with("task_statuses", project_id=1)


class TestAddComment:
    """Regression guard for #87: every comment path resolves ref→ID through the
    /<segment>/by_ref endpoint with `params=` (pre-v3, pytaigaclient's
    tasks.get_by_ref() sent a `query_params=` kwarg its own client rejected)."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_task_comment_uses_by_ref_endpoint_not_helper(self, session, mock_client):
        task = {"id": 200, "ref": 7, "version": 3}

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return self._project()

        mock_client.api.get.side_effect = api_get

        result = wf.add_comment("p", 7, "looks good", entity_type="task", session_id=session)

        assert result == {"status": "comment_added", "entity_type": "task", "ref": 7}
        # ref→ID resolution went through the raw endpoint with params=, not the helper.
        mock_client.api.get.assert_any_call("/tasks/by_ref", params={"ref": 7, "project": 1})
        # Comment posted to the task PATCH route with version for optimistic concurrency.
        mock_client.api.patch.assert_called_once_with(
            "/tasks/200", json={"comment": "looks good", "version": 3}
        )

    def test_story_comment_uses_userstories_by_ref(self, session, mock_client):
        story = {"id": 50, "ref": 12, "version": 1}

        def api_get(path, params=None):
            if "/userstories/by_ref" in str(path):
                return story
            return self._project()

        mock_client.api.get.side_effect = api_get

        wf.add_comment("p", 12, "ship it", entity_type="story", session_id=session)

        mock_client.api.get.assert_any_call("/userstories/by_ref", params={"ref": 12, "project": 1})
        mock_client.api.patch.assert_called_once_with(
            "/userstories/50", json={"comment": "ship it", "version": 1}
        )

    @pytest.mark.parametrize(
        "entity_type,segment",
        [
            ("user_story", "userstories"),  # alias of "story"
            ("issue", "issues"),
            ("epic", "epics"),
        ],
    )
    def test_remaining_entity_types_route_uniformly(
        self, session, mock_client, entity_type, segment
    ):
        item = {"id": 99, "ref": 5, "version": 2}

        def api_get(path, params=None):
            if f"/{segment}/by_ref" in str(path):
                return item
            return self._project()

        mock_client.api.get.side_effect = api_get

        wf.add_comment("p", 5, "note", entity_type=entity_type, session_id=session)

        mock_client.api.get.assert_any_call(f"/{segment}/by_ref", params={"ref": 5, "project": 1})
        mock_client.api.patch.assert_called_once_with(
            f"/{segment}/99", json={"comment": "note", "version": 2}
        )

    def test_raises_when_entity_not_found(self, session, mock_client):
        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return None
            return self._project()

        mock_client.api.get.side_effect = api_get
        with pytest.raises(ValueError, match="Task #7 not found"):
            wf.add_comment("p", 7, "x", entity_type="task", session_id=session)

    def test_rejects_invalid_entity_type(self, session, mock_client):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            wf.add_comment("p", 7, "x", entity_type="bogus", session_id=session)

    def test_rejects_empty_text(self, session, mock_client):
        with pytest.raises(ValueError, match="must not be empty"):
            wf.add_comment("p", 7, "   ", entity_type="task", session_id=session)


class TestBreakDownStory:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_empty_list_raises(self, session, mock_client):
        with pytest.raises(ValueError, match="tasks list cannot be empty"):
            wf.break_down_story("p", 42, [], session_id=session)

    def test_bulk_path_when_story_in_sprint(self, session, mock_client):
        """No overrides + story has milestone → bulk_create endpoint, one call."""
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 33,
                }
            },
        )
        route(
            mock_client,
            post={
                "/tasks/bulk_create": [
                    {"ref": 10, "subject": "design"},
                    {"ref": 11, "subject": "API"},
                    {"ref": 12, "subject": "tests"},
                ]
            },
        )
        result = wf.break_down_story("p", 42, ["design", "API", "tests"], session_id=session)
        assert result["status"] == "decomposed"
        assert result["tasks_created"] == 3
        # Single bulk POST, zero individual POST /tasks (an unrouted path would fail).
        mock_client.api.post.assert_called_once()
        (payload,) = sent(mock_client, "post", "/tasks/bulk_create")
        assert payload["us_id"] == 50
        assert payload["milestone_id"] == 33
        assert payload["bulk_tasks"] == "design\nAPI\ntests"

    def test_loop_path_when_story_in_backlog(self, session, mock_client):
        """No milestone on story → fall back to individual creates."""
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": None,
                }
            },
        )
        route(
            mock_client,
            post={
                "/tasks": Seq(
                    *[
                        {"ref": 10, "subject": "design"},
                        {"ref": 11, "subject": "API"},
                    ]
                )
            },
        )
        result = wf.break_down_story("p", 42, ["design", "API"], session_id=session)
        assert result["tasks_created"] == 2
        # No bulk endpoint (unrouted, would fail), one create per task.
        assert len(sent(mock_client, "post", "/tasks")) == 2

    def test_loop_path_when_per_task_overrides(self, session, mock_client):
        """Per-task overrides force the loop path even with a milestone."""
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 33,
                }
            },
        )
        mock_client.list_resources.return_value = [
            {
                "user": 7,
                "full_name": "Alice",
                "email": "a@x",
            }
        ]
        route(
            mock_client,
            post={
                "/tasks": Seq(
                    *[
                        {"ref": 10, "subject": "design"},
                        {"ref": 11, "subject": "API"},
                    ]
                )
            },
        )
        result = wf.break_down_story(
            "p",
            42,
            [
                {"subject": "design"},
                {"subject": "API", "assignee": "alice"},
            ],
            session_id=session,
        )
        assert result["tasks_created"] == 2
        # Per-task override forced the loop, not the bulk endpoint (unrouted, would fail).
        assert len(sent(mock_client, "post", "/tasks")) == 2
        # Second call assigned to Alice.
        assert sent(mock_client, "post", "/tasks")[1]["assigned_to"] == 7

    def test_rejects_malformed_entries(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": None,
                }
            },
        )
        with pytest.raises(ValueError, match="must be a non-empty string or a dict"):
            wf.break_down_story("p", 42, [{"no_subject": "oops"}], session_id=session)


# ---------------------------------------------------------------------------
# Regression: review round on PR #73
# ---------------------------------------------------------------------------


class TestCreateTaskSprintInheritance:
    """Regression: create_task must default the task's milestone to the parent's."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_inherits_parent_milestone_when_sprint_not_given(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 99,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.create_task("p", 42, "X", session_id=session)
        data = sent(mock_client, "post", "/tasks")[-1]
        # Critical: task lands in the parent story's sprint by default.
        assert data["milestone"] == 99

    def test_no_milestone_when_parent_in_backlog(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": None,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.create_task("p", 42, "X", session_id=session)
        data = sent(mock_client, "post", "/tasks")[-1]
        assert "milestone" not in data

    def test_sprint_zero_opts_out_even_if_parent_in_sprint(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 99,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.create_task("p", 42, "X", sprint=0, session_id=session)
        data = sent(mock_client, "post", "/tasks")[-1]
        assert "milestone" not in data

    def test_explicit_sprint_overrides_parent(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 99,
                }
            },
        )
        mock_client.list_resources.return_value = [{"id": 77, "name": "Sprint 6", "closed": False}]
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.create_task("p", 42, "X", sprint="Sprint 6", session_id=session)
        data = sent(mock_client, "post", "/tasks")[-1]
        assert data["milestone"] == 77


class TestBreakDownStoryOverrides:
    """Regression: loop path must honor all documented override keys."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_loop_supports_tags_due_date_blocked(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": None,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.break_down_story(
            "p",
            42,
            [
                {
                    "subject": "X",
                    "tags": ["a", "b"],
                    "due_date": "2026-06-30",
                    "blocked": True,
                }
            ],
            session_id=session,
        )
        data = sent(mock_client, "post", "/tasks")[-1]
        assert data["tags"] == ["a", "b"]
        assert data["due_date"] == "2026-06-30"
        assert data["is_blocked"] is True

    def test_rejects_unknown_override_key(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": None,
                }
            },
        )
        with pytest.raises(ValueError, match="Unknown per-task override keys"):
            wf.break_down_story("p", 42, [{"subject": "X", "color": "red"}], session_id=session)
        assert sent(mock_client, "post", "/tasks") == []

    def test_loop_path_inherits_parent_milestone(self, session, mock_client):
        """Tasks created via loop path (overrides present) should still inherit milestone."""
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 99,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        # Override forces loop path; milestone should still come through.
        wf.break_down_story(
            "p", 42, [{"subject": "X", "due_date": "2026-06-30"}], session_id=session
        )
        data = sent(mock_client, "post", "/tasks")[-1]
        assert data["milestone"] == 99


# ---------------------------------------------------------------------------
# Regression: round-2 review polish on PR #73
# ---------------------------------------------------------------------------


class TestCreateTaskSprintOptOutString:
    """The opt-out branch must accept both int 0 and string "0"."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_sprint_string_zero_opts_out(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 99,
                }
            },
        )
        route(mock_client, post={"/tasks": {"id": 1, "ref": 1, "subject": "X"}})
        wf.create_task("p", 42, "X", sprint="0", session_id=session)
        data = sent(mock_client, "post", "/tasks")[-1]
        # str "0" must opt out, NOT fall through to _resolve_sprint(..., "0")
        # which would look up a non-existent milestone.
        assert "milestone" not in data
        mock_client.list_resources.assert_not_called()


class TestBreakDownStoryBulkShapeWarn:
    """Bulk endpoint returning a non-list should warn and coerce to []."""

    def test_warns_and_coerces_on_dict_response(self, session, mock_client, caplog):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 42,
                    "milestone": 33,
                }
            },
        )
        # Simulate an unexpected response shape from /tasks/bulk_create.
        mock_client.api.post.return_value = {"unexpected": "shape"}

        with caplog.at_level("WARNING", logger="src.server.tools.tasks"):
            result = wf.break_down_story("p", 42, ["X", "Y"], session_id=session)

        assert result["tasks_created"] == 0
        assert result["tasks"] == []
        assert any(
            "Unexpected /tasks/bulk_create response shape" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# get_project_health
# ---------------------------------------------------------------------------


class TestGetProjectHealth:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _stats(self):
        return {
            "total_points": 100,
            "closed_points": 40,
            "assigned_points": 60,
            "total_milestones": 3,
            "speed": 2.5,
            "milestones": [
                {"name": "Sprint 1", "closed_points": 10},
                {"name": "Sprint 2", "closed_points": 30},
            ],
        }

    def _issues_stats(self):
        return {
            "total_issues": 20,
            "opened_issues": 14,
            "closed_issues": 6,
            "issues_per_priority": [
                {"name": "High", "count": 5},
                {"name": "Normal", "count": 9},
            ],
        }

    def _modules(self):
        return {
            "is_backlog_activated": True,
            "is_kanban_activated": False,
            "is_wiki_activated": True,
            "is_issues_activated": True,
            "is_epics_activated": False,
            "is_contact_activated": False,
        }

    def _setup_mocks(self, mock_client, stats=None, issues_stats=None, modules=None):
        def api_get(path, params=None):
            if "/projects/by_slug" in str(path):
                return self._project()
            if str(path).endswith("/stats"):
                return stats if stats is not None else self._stats()
            if str(path).endswith("/issues_stats"):
                return issues_stats if issues_stats is not None else self._issues_stats()
            if str(path).endswith("/modules"):
                return modules if modules is not None else self._modules()
            return self._project()

        mock_client.api.get.side_effect = api_get

    def test_returns_story_metrics(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert result["stories"]["total_points"] == 100
        assert result["stories"]["closed_points"] == 40
        assert result["stories"]["total_milestones"] == 3

    def test_returns_speed(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert result["stories"]["speed"] == 2.5

    def test_returns_issue_counts(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert result["issues"]["total"] == 20
        assert result["issues"]["open"] == 14
        assert result["issues"]["closed"] == 6
        assert result["issues"]["by_priority"]["High"] == 5

    def test_returns_velocity(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert len(result["velocity"]) == 2
        assert result["velocity"][0] == {"sprint": "Sprint 1", "closed_points": 10}

    def test_returns_active_modules_only(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert set(result["active_modules"]) == {"backlog", "wiki", "issues"}
        assert "kanban" not in result["active_modules"]

    def test_project_info_in_response(self, session, mock_client):
        self._setup_mocks(mock_client)
        result = wf.get_project_health("p", session_id=session)
        assert result["project"]["slug"] == "p"
        assert result["project"]["id"] == 1

    def test_tolerates_none_stats_and_issues_stats(self, session, mock_client):
        def api_get(path, params=None):
            if "/projects/by_slug" in str(path):
                return self._project()
            if str(path).endswith(("/stats", "/issues_stats", "/modules")):
                return None
            return self._project()

        mock_client.api.get.side_effect = api_get
        result = wf.get_project_health("p", session_id=session)
        assert result["stories"]["total_points"] == 0
        assert result["stories"]["speed"] == 0
        assert result["issues"]["total"] == 0
        assert result["velocity"] == []
        assert result["active_modules"] == []


# ---------------------------------------------------------------------------
# get_project_activity
# ---------------------------------------------------------------------------


class TestGetProjectActivity:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _event(
        self, event_type="userstories.userstory.change", actor="Alice", fields=None, comment=""
    ):
        return {
            "event_type": event_type,
            "created": "2026-06-05T10:00:00Z",
            "object_id": 42,
            "data": {
                "user": {"name": actor},
                "values_diff": {f: ["old", "new"] for f in (fields or [])},
                "comment": comment,
            },
        }

    def _setup(self, mock_client, events):
        def api_get(path, params=None):
            if "/projects/by_slug" in str(path):
                return self._project()
            return events

        mock_client.api.get.side_effect = api_get

    def test_returns_events_list(self, session, mock_client):
        events = [self._event(fields=["status"])]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert result["count"] == 1
        assert result["events"][0]["entity"] == "user_story"
        assert result["events"][0]["action"] == "change"
        assert result["events"][0]["actor"] == "Alice"

    def test_changed_fields_extracted(self, session, mock_client):
        events = [self._event(fields=["status", "assigned_to"])]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert "status" in result["events"][0]["changed_fields"]
        assert "assigned_to" in result["events"][0]["changed_fields"]

    def test_comment_included(self, session, mock_client):
        events = [self._event(comment="Great progress!")]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["comment"] == "Great progress!"

    def test_comment_truncated_at_200(self, session, mock_client):
        long_comment = "x" * 300
        events = [self._event(comment=long_comment)]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert len(result["events"][0]["comment"]) == 200

    def test_no_changed_fields_key_when_empty(self, session, mock_client):
        events = [self._event()]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert "changed_fields" not in result["events"][0]

    def test_entity_map_task(self, session, mock_client):
        events = [self._event(event_type="tasks.task.create")]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["entity"] == "task"
        assert result["events"][0]["action"] == "create"

    def test_invalid_limit_raises(self, session, mock_client):
        with pytest.raises(ValueError, match="limit must be between"):
            wf.get_project_activity("p", limit=0, session_id=session)

    def test_limit_too_large_raises(self, session, mock_client):
        with pytest.raises(ValueError, match="limit must be between"):
            wf.get_project_activity("p", limit=101, session_id=session)

    def test_non_list_response_coerced_to_empty(self, session, mock_client):
        def api_get(path, params=None):
            if "/projects/by_slug" in str(path):
                return self._project()
            return {"unexpected": "dict"}

        mock_client.api.get.side_effect = api_get
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"] == []
        assert result["count"] == 0

    def test_actor_falls_back_to_username(self, session, mock_client):
        event = self._event()
        event["data"]["user"] = {"username": "bob"}
        self._setup(mock_client, [event])
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["actor"] == "bob"

    def test_actor_unknown_when_no_user_info(self, session, mock_client):
        event = self._event()
        event["data"]["user"] = {}
        self._setup(mock_client, [event])
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["actor"] == "unknown"

    def test_when_and_object_id_propagated(self, session, mock_client):
        events = [self._event()]
        self._setup(mock_client, events)
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["when"] == "2026-06-05T10:00:00Z"
        assert result["events"][0]["object_id"] == 42

    def test_empty_event_type_yields_unknown_entity(self, session, mock_client):
        event = self._event(event_type="")
        self._setup(mock_client, [event])
        result = wf.get_project_activity("p", session_id=session)
        assert result["events"][0]["entity"] == "unknown"
        assert result["events"][0]["action"] == "unknown"


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def _me(self):
        return {
            "id": 42,
            "username": "jdoe",
            "full_name_display": "Jane D",
            "full_name": "Jane Q. Doe",
            "email": "jdoe@example.com",
            "bio": "Platform lead",
            "photo": "https://cdn.example.com/jdoe.jpg",
        }

    def test_returns_identity_fields(self, session, mock_client):
        route(mock_client, get={"/users/me": self._me()})
        result = wf.get_current_user(session_id=session)
        assert result["id"] == 42
        assert result["username"] == "jdoe"
        assert result["full_name"] == "Jane D"
        assert result["email"] == "jdoe@example.com"

    def test_prefers_full_name_display(self, session, mock_client):
        me = self._me()
        me["full_name_display"] = "Display Name"
        me["full_name"] = "Other Name"
        route(mock_client, get={"/users/me": me})
        result = wf.get_current_user(session_id=session)
        assert result["full_name"] == "Display Name"

    def test_falls_back_to_full_name(self, session, mock_client):
        me = self._me()
        me["full_name_display"] = None
        route(mock_client, get={"/users/me": me})
        result = wf.get_current_user(session_id=session)
        assert result["full_name"] == "Jane Q. Doe"

    def test_optional_fields_none_when_missing(self, session, mock_client):
        route(mock_client, get={"/users/me": {"id": 1, "username": "x", "email": "x@example.com"}})
        result = wf.get_current_user(session_id=session)
        assert result["bio"] is None
        assert result["photo"] is None

    def test_unauthenticated_raises(self):
        with pytest.raises((ValueError, PermissionError)):
            wf.get_current_user(session_id="nonexistent")


# ---------------------------------------------------------------------------
# Status-id resolution on reads (#95) — search + get_* by ref
# ---------------------------------------------------------------------------


class TestStatusMap:
    def test_builds_id_to_name_isclosed_map(self, mock_client):
        mock_client.list_resources.return_value = [
            {"id": 57, "name": "New", "is_closed": False},
            {"id": 60, "name": "Closed", "is_closed": True},
        ]
        result = wf._status_map(mock_client, 1, "issue", "sess")
        assert result == {
            57: {"name": "New", "is_closed": False},
            60: {"name": "Closed", "is_closed": True},
        }

    def test_shares_cache_with_resolve_status(self, mock_client):
        mock_client.list_resources.return_value = [{"id": 60, "name": "Closed", "is_closed": True}]
        wf._resolve_status(mock_client, 1, "issue", "Closed", "sess")
        wf._status_map(mock_client, 1, "issue", "sess")
        # One fetch shared between resolver and map (same session cache key).
        mock_client.list_resources.assert_called_once_with("issue_statuses", project_id=1)

    def test_unknown_entity_type_returns_empty(self, mock_client):
        assert wf._status_map(mock_client, 1, "wiki", "sess") == {}
        mock_client.list_resources.assert_not_called()


class TestSearch:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_resolves_raw_status_ids_to_name_and_is_closed(self, session, mock_client):
        search_result = {
            "count": 2,
            "issues": [{"ref": 1212, "subject": "Bug", "status": 57}],
            "userstories": [{"ref": 1114, "subject": "Story", "status": 67}],
            "tasks": [],
            "epics": [],
            "wikipages": [{"id": 9, "title": "Page"}],
        }

        def api_get(path, params=None):
            if "/search" in str(path):
                return search_result
            return self._project()

        mock_client.api.get.side_effect = api_get
        # _resolved is called in order story, task, issue, epic; only non-empty
        # groups fetch a status table → userstory_statuses then issue_statuses.
        mock_client.list_resources.side_effect = [
            [{"id": 67, "name": "In progress", "is_closed": False}],  # userstory_statuses
            [{"id": 57, "name": "New", "is_closed": False}],  # issue_statuses
        ]

        result = wf.search("p", "anything", session_id=session)

        assert result["issues"][0]["status"] == "New"
        assert result["issues"][0]["is_closed"] is False
        assert result["user_stories"][0]["status"] == "In progress"
        assert result["user_stories"][0]["is_closed"] is False
        # Empty groups don't trigger a status fetch.
        assert mock_client.list_resources.call_count == 2

    def test_leaves_status_untouched_when_id_unknown(self, session, mock_client):
        search_result = {
            "count": 1,
            "issues": [{"ref": 5, "subject": "Bug", "status": 999}],
            "userstories": [],
            "tasks": [],
            "epics": [],
            "wikipages": [],
        }

        def api_get(path, params=None):
            if "/search" in str(path):
                return search_result
            return self._project()

        mock_client.api.get.side_effect = api_get
        mock_client.list_resources.return_value = [{"id": 57, "name": "New", "is_closed": False}]

        result = wf.search("p", "x", session_id=session)
        # Unknown id is left as-is rather than mislabelled, but is_closed is still
        # present (None) so the output shape stays uniform across items.
        assert result["issues"][0]["status"] == 999
        assert result["issues"][0]["is_closed"] is None

    def test_unknown_id_preserves_existing_is_closed(self, session, mock_client):
        # If the payload already carries is_closed, an unknown status id must not
        # clobber it — the status table is authoritative only when it has the id.
        search_result = {
            "count": 1,
            "issues": [{"ref": 5, "subject": "Bug", "status": 999, "is_closed": True}],
            "userstories": [],
            "tasks": [],
            "epics": [],
            "wikipages": [],
        }

        def api_get(path, params=None):
            if "/search" in str(path):
                return search_result
            return self._project()

        mock_client.api.get.side_effect = api_get
        mock_client.list_resources.return_value = [{"id": 57, "name": "New", "is_closed": False}]

        result = wf.search("p", "x", session_id=session)
        assert result["issues"][0]["status"] == 999
        assert result["issues"][0]["is_closed"] is True

    def test_resolves_epic_status(self, session, mock_client):
        search_result = {
            "count": 1,
            "issues": [],
            "userstories": [],
            "tasks": [],
            "epics": [{"ref": 42, "subject": "Epic", "status": 30}],
            "wikipages": [],
        }

        def api_get(path, params=None):
            if "/search" in str(path):
                return search_result
            return self._project()

        mock_client.api.get.side_effect = api_get
        mock_client.list_resources.return_value = [
            {"id": 30, "name": "In progress", "is_closed": False}
        ]

        result = wf.search("p", "x", session_id=session)
        assert result["epics"][0]["status"] == "In progress"
        assert result["epics"][0]["is_closed"] is False
        # Resolved against the epic_statuses table specifically.
        mock_client.list_resources.assert_called_once_with("epic_statuses", project_id=1)


class TestStorySummary:
    """_story_summary shape — the sprint field in particular."""

    def test_sprint_comes_from_flat_milestone_name(self):
        # Taiga sends no milestone_extra_info on user stories — the sprint is the
        # flat milestone_name (slug as fallback). Reading the phantom key returned
        # None for every story that had a sprint.
        assert (
            wf._story_summary({"ref": 1, "milestone": 85, "milestone_name": "Sprint 9"})["sprint"]
            == "Sprint 9"
        )

    def test_sprint_falls_back_to_slug(self):
        assert (
            wf._story_summary({"ref": 2, "milestone": 85, "milestone_slug": "sprint-9"})["sprint"]
            == "sprint-9"
        )

    def test_sprint_is_none_when_story_has_no_milestone(self):
        assert wf._story_summary({"ref": 3})["sprint"] is None

    def test_epics_empty_when_taiga_sends_null(self):
        # Taiga sends `epics: null`, not [], for a story in no epic.
        assert wf._story_summary({"ref": 3, "epics": None})["epics"] == []
        assert wf._story_summary({"ref": 3})["epics"] == []

    def test_epics_lists_every_link(self):
        story = {
            "ref": 4,
            "project": 13,
            "epics": [
                {"id": 223, "ref": 458, "subject": "One", "color": "#d3d7cf",
                 "project": {"id": 13, "name": "P", "slug": "p"}},
                {"id": 224, "ref": 459, "subject": "Two", "color": "#c4a000",
                 "project": {"id": 13, "name": "P", "slug": "p"}},
            ],
        }  # fmt: skip
        assert wf._story_summary(story)["epics"] == [
            {"ref": 458, "subject": "One"},
            {"ref": 459, "subject": "Two"},
        ]

    def test_epic_of_another_project_names_that_project(self):
        story = {
            "ref": 4,
            "project": 13,
            "epics": [{"id": 9, "ref": 2, "subject": "X", "project": {"id": 8, "slug": "other"}}],
        }
        assert wf._story_summary(story)["epics"] == [{"ref": 2, "subject": "X", "project": "other"}]


class TestTaskSummary:
    def test_story_ref_comes_from_user_story_extra_info(self):
        task = {"ref": 7, "user_story": 480, "user_story_extra_info": {"id": 480, "ref": 1}}
        assert wf._task_summary(task)["story_ref"] == 1

    def test_story_ref_is_none_for_a_task_without_story(self):
        assert wf._task_summary({"ref": 7, "user_story_extra_info": None})["story_ref"] is None


class TestGetStory:
    def test_returns_resolved_summary(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "ref": 3,
                    "subject": "Story A",
                    "status_extra_info": {"name": "In Progress"},
                    "assigned_to_extra_info": {"full_name_display": "Alice"},
                    "is_closed": False,
                }
            },
        )
        result = wf.get_story("p", 3, session_id=session)
        assert result["status"] == "In Progress"
        assert result["is_closed"] is False
        assert result["assignee"] == "Alice"
        mock_client.api.get.assert_any_call("/userstories/by_ref", params={"ref": 3, "project": 1})

    def test_raises_when_not_found(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(mock_client, get={"/userstories/by_ref": None})
        with pytest.raises(ValueError, match="not found"):
            wf.get_story("p", 99, session_id=session)


class TestGetTask:
    def test_uses_by_ref_endpoint_not_helper(self, session, mock_client):
        project = {"id": 1, "slug": "p", "name": "P"}
        task = {
            "ref": 7,
            "subject": "Task 1",
            "status_extra_info": {"name": "Done"},
            "is_closed": True,
        }

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return project

        mock_client.api.get.side_effect = api_get
        result = wf.get_task("p", 7, session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True
        mock_client.api.get.assert_any_call("/tasks/by_ref", params={"ref": 7, "project": 1})

    def test_raises_when_not_found(self, session, mock_client):
        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return None
            return {"id": 1, "slug": "p", "name": "P"}

        mock_client.api.get.side_effect = api_get
        with pytest.raises(ValueError, match="not found"):
            wf.get_task("p", 99, session_id=session)


class TestGetIssue:
    @staticmethod
    def _attr_lists():
        """side_effect for list_resources — the project's issue attribute tables."""
        tables = {
            "priorities": [{"id": 25, "name": "Low"}, {"id": 27, "name": "High"}],
            "severities": [{"id": 43, "name": "Normal"}, {"id": 45, "name": "Critical"}],
            "issue_types": [{"id": 25, "name": "Bug"}, {"id": 26, "name": "Question"}],
        }
        return lambda resource_type, **kwargs: tables[resource_type]

    def test_returns_resolved_summary(self, session, mock_client):
        # REAL Taiga shape: priority/severity/type come back as bare integer IDs.
        # Taiga publishes no priority_extra_info/severity_extra_info/type_extra_info
        # (only assigned_to/owner/project/status), so the names must come from a
        # reverse lookup against the project's attribute tables. Deliberately no
        # *_extra_info keys here — an earlier fixture supplied them, which made this
        # test pass against a payload Taiga never sends while production returned
        # None for all three.
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.side_effect = self._attr_lists()
        route(
            mock_client,
            get={
                "/issues/by_ref": {
                    "ref": 1212,
                    "subject": "Bug",
                    "status_extra_info": {"name": "New"},
                    "priority": 27,
                    "severity": 45,
                    "type": 25,
                    "assigned_to_extra_info": None,
                    "is_closed": False,
                }
            },
        )
        result = wf.get_issue("p", 1212, session_id=session)
        assert result["status"] == "New"
        assert result["is_closed"] is False
        assert result["priority"] == "High"
        assert result["severity"] == "Critical"
        assert result["type"] == "Bug"
        mock_client.api.get.assert_any_call("/issues/by_ref", params={"ref": 1212, "project": 1})

    def test_unknown_attribute_id_falls_back_to_raw_id(self, session, mock_client):
        # An ID absent from the project's table (stale cache, cross-project ref)
        # must degrade to the raw ID — mirroring the `status` field — rather than
        # raising or silently vanishing.
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.side_effect = self._attr_lists()
        route(
            mock_client,
            get={
                "/issues/by_ref": {
                    "ref": 1213,
                    "subject": "Odd",
                    "status_extra_info": {"name": "New"},
                    "priority": 9999,
                    "severity": None,
                    "type": 25,
                }
            },
        )
        result = wf.get_issue("p", 1213, session_id=session)
        assert result["priority"] == 9999
        assert result["severity"] is None
        assert result["type"] == "Bug"

    def test_partial_resolver_context_raises(self):
        # All-or-nothing: a caller that passes client+project_id but forgets
        # session_id must not silently receive raw IDs that look resolved.
        with pytest.raises(ValueError, match="passed together"):
            wf._issue_summary({"ref": 1}, client=MagicMock(), project_id=1)

    def test_summary_without_resolver_context_returns_raw_ids(self):
        # _issue_summary is also reachable without client/project context. It must
        # then surface the raw IDs, never a misleading None.
        result = wf._issue_summary(
            {"ref": 7, "subject": "s", "priority": 27, "severity": 45, "type": 25}
        )
        assert result["priority"] == 27
        assert result["severity"] == 45
        assert result["type"] == 25

    def test_raises_when_not_found(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(mock_client, get={"/issues/by_ref": None})
        with pytest.raises(ValueError, match="not found"):
            wf.get_issue("p", 99, session_id=session)

    def test_is_closed_falls_back_to_status_extra_info(self, session, mock_client):
        # Payload with no top-level is_closed — closed-ness must be read off the
        # status row so the triage signal ("is it closed?") isn't silently False.
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            get={
                "/issues/by_ref": {
                    "ref": 60,
                    "subject": "Done bug",
                    "status_extra_info": {"name": "Closed", "is_closed": True},
                }
            },
        )
        result = wf.get_issue("p", 60, session_id=session)
        assert result["status"] == "Closed"
        assert result["is_closed"] is True


# ---------------------------------------------------------------------------
# Sprint write tools — payload-shape regression cover (P0)
# ---------------------------------------------------------------------------


class TestPlanSprint:
    def test_creates_sprint_and_moves_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            post={
                "/milestones": {
                    "id": 9,
                    "name": "S1",
                    "estimated_start": "2026-01-01",
                    "estimated_finish": "2026-01-14",
                },
                "/userstories/bulk_update_milestone": {},
            },
        )
        # _resolve_story_refs → list_resources("user_stories")
        mock_client.list_resources.return_value = [
            {"ref": 12, "id": 120},
            {"ref": 15, "id": 150},
        ]
        result = wf.plan_sprint(
            "p", "S1", "2026-01-01", "2026-01-14", story_refs=[12, 15], session_id=session
        )
        assert result["status"] == "created"
        assert result["sprint"]["id"] == 9
        assert result["stories_assigned"] == 2
        assert sent(mock_client, "post", "/milestones") == [
            {
                "project": 1,
                "name": "S1",
                "estimated_start": "2026-01-01",
                "estimated_finish": "2026-01-14",
            }
        ]
        assert sent(mock_client, "post", "/userstories/bulk_update_milestone") == [
            {
                "project_id": 1,
                "milestone_id": 9,
                "bulk_stories": [{"us_id": 120, "order": 0}, {"us_id": 150, "order": 1}],
            }
        ]

    def test_creates_sprint_without_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            post={
                "/milestones": {
                    "id": 9,
                    "name": "S1",
                    "estimated_start": "2026-01-01",
                    "estimated_finish": "2026-01-14",
                }
            },
        )
        result = wf.plan_sprint("p", "S1", "2026-01-01", "2026-01-14", session_id=session)
        assert result["stories_assigned"] == 0
        # Only the sprint itself is created: no bulk move (unrouted, would fail).
        mock_client.api.post.assert_called_once()


class TestMoveToSprint:
    def test_moves_stories_into_sprint(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "milestones": [{"id": 9, "name": "S1"}],
                "user_stories": [{"ref": 12, "id": 120}, {"ref": 15, "id": 150}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.move_to_sprint("p", [12, 15], "S1", session_id=session)
        assert result == {"status": "moved", "sprint": "S1", "stories_moved": 2, "refs": [12, 15]}
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_milestone",
            json={
                "project_id": 1,
                "milestone_id": 9,
                "bulk_stories": [{"us_id": 120, "order": 0}, {"us_id": 150, "order": 1}],
            },
        )

    def test_unknown_sprint_raises(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [{"id": 9, "name": "S1"}]
        with pytest.raises(ValueError, match="Sprint 'S9' not found"):
            wf.move_to_sprint("p", [12], "S9", session_id=session)


class TestCloseSprint:
    def test_closes_named_sprint(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [{"id": 9, "name": "S1", "version": 3}]
        route(mock_client, patch={"/milestones/9": {"id": 9, "name": "S1"}})
        result = wf.close_sprint("p", "S1", session_id=session)
        assert result == {"status": "closed", "sprint": "S1", "id": 9}
        assert sent(mock_client, "patch", "/milestones/9") == [{"closed": True, "version": 3}]


class TestCreateEpic:
    def test_creates_epic_and_links_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            post={
                "/epics": {"id": 7, "ref": 70, "subject": "Big"},
                "/epics/7/related_userstories": {},
            },
        )
        mock_client.list_resources.return_value = [{"ref": 12, "id": 120}]  # user_stories
        result = wf.create_epic("p", "Big", story_refs=[12], session_id=session)
        assert result["status"] == "created"
        assert result["id"] == 7
        assert result["stories_linked"] == 1
        assert sent(mock_client, "post", "/epics") == [{"project": 1, "subject": "Big"}]
        assert sent(mock_client, "post", "/epics/7/related_userstories") == [
            {"epic": 7, "user_story": 120}
        ]

    def test_links_each_story_and_never_uses_bulk_create(self, session, mock_client):
        """Regression for #143: /related_userstories/bulk_create creates NEW stories
        from subject lines, so linking must be one POST per story."""
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            post={
                "/epics": {"id": 7, "ref": 70, "subject": "Big"},
                "/epics/7/related_userstories": {},
            },
        )
        mock_client.list_resources.return_value = [{"ref": 12, "id": 120}, {"ref": 13, "id": 130}]
        result = wf.create_epic("p", "Big", story_refs=[12, 13], session_id=session)
        assert result["stories_linked"] == 2
        assert mock_client.api.post.call_args_list == [
            call("/epics", json={"project": 1, "subject": "Big"}),
            call("/epics/7/related_userstories", json={"epic": 7, "user_story": 120}),
            call("/epics/7/related_userstories", json={"epic": 7, "user_story": 130}),
        ]
        for c in mock_client.api.post.call_args_list:
            assert "bulk_create" not in c.args[0]

    def test_partial_link_failure_raises_with_epic_ref(self, session, mock_client):
        """A failed link must not be reported as success (#143 reported false success)."""
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            post={
                "/epics": {"id": 7, "ref": 70, "subject": "Big"},
                "/epics/7/related_userstories": Seq(None, TaigaException("boom")),
            },
        )
        mock_client.list_resources.return_value = [{"ref": 12, "id": 120}, {"ref": 13, "id": 130}]
        with pytest.raises(TaigaException, match=r"Epic #70 .*linked only 1 of 2.*#13: boom"):
            wf.create_epic("p", "Big", story_refs=[12, 13], session_id=session)

    def test_creates_epic_minimal(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(mock_client, post={"/epics": {"id": 7, "ref": 70, "subject": "Big"}})
        result = wf.create_epic("p", "Big", session_id=session)
        assert result["stories_linked"] == 0
        # Only the epic is created: no link POST (unrouted, would fail).
        mock_client.api.post.assert_called_once()

    def test_create_epic_with_color_and_assignee(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [  # memberships for _resolve_user
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@x",
            }
        ]
        route(mock_client, post={"/epics": {"id": 7, "ref": 70, "subject": "Big"}})
        wf.create_epic("p", "Big", color="#fff", assignee="bob", session_id=session)
        assert sent(mock_client, "post", "/epics") == [
            {"project": 1, "subject": "Big", "color": "#fff", "assigned_to": 7}
        ]


class TestAssignItem:
    @pytest.mark.parametrize(
        "entity_type,segment,item_id,version",
        [
            ("story", "userstories", 100, 2),
            ("issue", "issues", 300, 1),
            ("task", "tasks", 400, 3),
            ("epic", "epics", 500, 4),
        ],
    )
    def test_assigns_each_entity_type(
        self, session, mock_client, entity_type, segment, item_id, version
    ):
        """One PATCH shape for every type: {version, assigned_to} on /<segment>/<id>.

        Pre-v3 the resource helpers' edit() signatures diverged (data={...} for
        tasks/issues, **kwargs for stories/epics, #123); the raw PATCH removes that.
        """
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [  # memberships
            {"user": 7, "full_name": "Bob", "email": "bob@x"}
        ]
        route(
            mock_client,
            get={f"/{segment}/by_ref": {"id": item_id, "version": version}},
            patch={
                f"/{segment}/{item_id}": {"assigned_to_extra_info": {"full_name_display": "Bob"}}
            },
        )
        result = wf.assign_item("p", 5, "bob", entity_type=entity_type, session_id=session)
        assert result == {"ref": 5, "entity_type": entity_type, "assigned_to": "Bob"}
        mock_client.api.get.assert_any_call(f"/{segment}/by_ref", params={"ref": 5, "project": 1})
        assert sent(mock_client, "patch", f"/{segment}/{item_id}") == [
            {"version": version, "assigned_to": 7}
        ]

    def test_invalid_entity_type_raises(self, session, mock_client):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            wf.assign_item("p", 5, "bob", entity_type="bogus", session_id=session)


# ---------------------------------------------------------------------------
# Read / composite tools (P0)
# ---------------------------------------------------------------------------


class TestGetProjectOverview:
    def test_assembles_overview(self, session, mock_client):
        mock_client.api.get.return_value = {
            "id": 1,
            "slug": "p",
            "name": "P",
            "description": "d",
            "is_private": False,
        }

        def lr(resource, **kw):
            return {
                # REAL membership shape: no user_extra_info, no username. Identity
                # is flat, and `email` is often blank while `user_email` is set.
                "memberships": [
                    {
                        "user": 8,
                        "user_email": "bob@example.com",
                        "email": "",
                        "full_name": "Bob",
                        "role_name": "Dev",
                        "is_admin": False,
                    }
                ],
                # Wide date range so the "covers today" test is deterministic.
                "milestones": [
                    {
                        "id": 9,
                        "name": "S1",
                        "closed": False,
                        "estimated_start": "2020-01-01",
                        "estimated_finish": "2099-12-31",
                    }
                ],
                "user_stories": [
                    {"status_extra_info": {"name": "New"}},
                    {"status_extra_info": {"name": "New"}},
                    {"status_extra_info": {"name": "Done"}},
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.get_project_overview("p", session_id=session)
        assert result["project"]["slug"] == "p"
        assert result["total_stories"] == 3
        assert result["stories_by_status"] == {"New": 2, "Done": 1}
        assert result["active_sprint"]["name"] == "S1"
        # Sourced from user_email, not the blank `email` and not a phantom
        # user_extra_info.username (which Taiga never sends on memberships).
        assert result["team"][0]["email"] == "bob@example.com"
        assert result["team"][0]["full_name"] == "Bob"

    def test_no_active_sprint_when_none_cover_today(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "memberships": [],
                "milestones": [
                    {
                        "id": 9,
                        "name": "Old",
                        "closed": False,
                        "estimated_start": "2000-01-01",
                        "estimated_finish": "2000-12-31",
                    }
                ],
                "user_stories": [],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.get_project_overview("p", session_id=session)
        assert result["active_sprint"] is None
        assert result["total_stories"] == 0


class TestBrowseBacklog:
    def test_minimal_backlog_filters_milestone_null(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [
            {"ref": 12, "subject": "A", "status_extra_info": {"name": "New"}}
        ]
        result = wf.browse_backlog("p", session_id=session)
        assert result[0]["ref"] == 12
        _, kwargs = mock_client.list_resources.call_args
        assert kwargs["milestone"] == "null"

    def test_filter_by_epic_resolves_ref_to_id(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            {"id": 77},  # /epics/by_ref
        ]
        mock_client.list_resources.return_value = []
        wf.browse_backlog("p", epic=7, session_id=session)
        _, kwargs = mock_client.list_resources.call_args
        assert kwargs["epic"] == 77
        assert kwargs["milestone"] == "null"


class TestGetTeamWorkload:
    def test_aggregates_per_member_whole_project(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "user_stories": [
                    {"assigned_to_extra_info": {"full_name_display": "Bob"}, "is_blocked": True},
                    {"assigned_to_extra_info": {"full_name_display": "Bob"}},
                    {"assigned_to_extra_info": None},  # unassigned
                ],
                "tasks": [
                    {"assigned_to_extra_info": {"full_name_display": "Bob"}},
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.get_team_workload("p", session_id=session)
        assert result["sprint"] is None
        team = {row["member"]: row for row in result["team"]}
        assert team["Bob"] == {
            "member": "Bob",
            "stories": 2,
            "tasks": 1,
            "blocked_stories": 1,
        }
        assert team["(unassigned)"]["stories"] == 1

    def test_sprint_filter_applied(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "milestones": [{"id": 9, "name": "S1"}],
                "user_stories": [],
                "tasks": [],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.get_team_workload("p", sprint="S1", session_id=session)
        assert result["sprint"] == "S1"
        # stories + tasks both fetched with the milestone filter
        story_call = [c for c in mock_client.list_resources.call_args_list if c.args[0] == "tasks"][
            0
        ]
        assert story_call.kwargs["milestone"] == 9


class TestGetWiki:
    def test_get_page_by_slug(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            {"slug": "home", "content": "hi", "id": 9},
        ]
        result = wf.get_wiki("p", slug="home", session_id=session)
        assert result == {"slug": "home", "content": "hi", "id": 9}

    def test_list_pages_when_no_slug(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [
            {"slug": "home", "id": 9},
            {"slug": "spec", "id": 10},
        ]
        result = wf.get_wiki("p", session_id=session)
        assert result == [{"slug": "home", "id": 9}, {"slug": "spec", "id": 10}]

    def test_page_not_found_raises(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            None,
        ]
        with pytest.raises(ValueError, match="Wiki page 'home' not found"):
            wf.get_wiki("p", slug="home", session_id=session)


class TestAutoLogin:
    """The lifespan's startup login (#197)."""

    def _run_lifespan(self, monkeypatch, wrapper):
        app_module = sys.modules["src.server.app"]
        monkeypatch.setattr(core, "auto_login_error", None)
        monkeypatch.setattr(taiga_client, "TaigaClientWrapper", lambda host: wrapper)
        monkeypatch.setattr(type(app_module.settings), "has_credentials", property(lambda s: True))
        monkeypatch.setattr(type(app_module.settings), "get_username_value", lambda s: "u")
        monkeypatch.setattr(type(app_module.settings), "get_password_value", lambda s: "p")
        errors = []

        async def run():
            async with app_module.server_lifespan(MagicMock()):
                try:
                    errors.append(wf._get_session_id())
                except ValueError as e:
                    errors.append(e)

        asyncio.run(run())
        return errors[0]

    def test_failed_auto_login_is_reported_by_tools(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.side_effect = TaigaNotFoundError(404, "Not found.")
        error = self._run_lifespan(monkeypatch, wrapper)
        assert isinstance(error, ValueError)
        assert "Auto-login at startup failed: TaigaNotFoundError" in str(error)
        assert "TAIGA_API_URL" in str(error)
        assert "No session_id provided" not in str(error)

    def test_successful_auto_login_gives_the_default_session(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.return_value = True
        assert self._run_lifespan(monkeypatch, wrapper) == wf.DEFAULT_SESSION_ID
        assert core.auto_login_error is None


class TestLogin:
    def test_login_success_creates_session(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.return_value = True
        # login() and the lifespan look TaigaClientWrapper up via src.taiga_client.
        monkeypatch.setattr(taiga_client, "TaigaClientWrapper", lambda host: wrapper)
        result = wf.login(host="https://taiga.example", username="u", password="p")
        assert "session_id" in result
        assert wf.active_sessions[result["session_id"]] is wrapper
        wrapper.login.assert_called_once_with(username="u", password="p")

    def test_login_failure_raises(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.return_value = False
        # login() and the lifespan look TaigaClientWrapper up via src.taiga_client.
        monkeypatch.setattr(taiga_client, "TaigaClientWrapper", lambda host: wrapper)
        # The bare RuntimeError("Login failed.") is re-wrapped by the generic
        # handler into the "unexpected server error" message.
        with pytest.raises(RuntimeError, match="unexpected server error"):
            wf.login(host="https://taiga.example", username="u", password="p")

    def test_login_missing_credentials_raises(self, monkeypatch):
        # settings is a pydantic model — patch the methods on the class, not the instance.
        monkeypatch.setattr(type(wf.settings), "get_username_value", lambda self: "")
        monkeypatch.setattr(type(wf.settings), "get_password_value", lambda self: "")
        with pytest.raises(ValueError, match="Credentials required"):
            wf.login(host="https://taiga.example")


# ---------------------------------------------------------------------------
# Infrastructure helpers (P1)
# ---------------------------------------------------------------------------


class TestResolveTransportWorkflow:
    def test_defaults_to_stdio(self):
        assert wf._resolve_transport(argv=[], env={}) == "stdio"

    def test_sse_flag(self):
        assert wf._resolve_transport(argv=["s.py", "--sse"], env={}) == "sse"

    def test_streamable_http_flag(self):
        assert (
            wf._resolve_transport(argv=["s.py", "--streamable-http"], env={}) == "streamable-http"
        )

    def test_env_transport(self):
        assert wf._resolve_transport(argv=[], env={"TAIGA_TRANSPORT": "sse"}) == "sse"

    def test_env_case_insensitive(self):
        assert wf._resolve_transport(argv=[], env={"TAIGA_TRANSPORT": "SSE"}) == "sse"

    def test_flag_overrides_env(self):
        assert (
            wf._resolve_transport(argv=["s.py", "--sse"], env={"TAIGA_TRANSPORT": "stdio"}) == "sse"
        )

    def test_unknown_env_falls_back_to_stdio(self):
        assert wf._resolve_transport(argv=[], env={"TAIGA_TRANSPORT": "bogus"}) == "stdio"


class TestExecuteTaigaOperation:
    """The wrapper around every tool body — error classification."""

    def test_reraises_api_error_unchanged(self):
        # The client builds a readable error_detail itself (field errors included);
        # the wrapper must pass the same exception object through.
        err = TaigaAPIError(400, "field: required", {"field": ["required"]})

        def op():
            raise err

        with pytest.raises(TaigaAPIError) as ei:
            wf._execute_taiga_operation("op", op)
        assert ei.value is err
        assert ei.value.error_detail == "field: required"

    def test_value_error_passes_through(self):
        def op():
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            wf._execute_taiga_operation("op", op)

    def test_taiga_exception_reraised(self):
        def op():
            raise TaigaException("taiga boom")

        with pytest.raises(TaigaException, match="taiga boom"):
            wf._execute_taiga_operation("op", op)

    def test_generic_exception_wrapped_in_runtime_error(self):
        def op():
            raise KeyError("k")

        with pytest.raises(RuntimeError, match="Server error in op"):
            wf._execute_taiga_operation("op", op)


# ---------------------------------------------------------------------------
# Branch cover for already-smoke-tested tools (P2)
# ---------------------------------------------------------------------------


class TestResolveIssueAttribute:
    def test_unknown_attribute_type_raises(self, session, mock_client):
        with pytest.raises(ValueError, match="Unknown attribute type"):
            wf._resolve_issue_attribute(mock_client, 1, "bogus", "x", session)

    def test_attribute_not_found_raises(self, session, mock_client):
        mock_client.list_resources.return_value = [{"id": 1, "name": "High"}]
        with pytest.raises(ValueError, match="Priority 'Low' not found"):
            wf._resolve_issue_attribute(mock_client, 1, "priority", "Low", session)


class TestBrowseBacklogFilters:
    def test_status_assignee_tags_filters(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "userstory_statuses": [{"id": 2, "name": "New"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                    }
                ],
                "user_stories": [],
            }[resource]

        mock_client.list_resources.side_effect = lr
        wf.browse_backlog("p", status="New", assignee="bob", tags="urgent", session_id=session)
        us_call = [
            c for c in mock_client.list_resources.call_args_list if c.args[0] == "user_stories"
        ][0]
        assert us_call.kwargs["status"] == 2
        assert us_call.kwargs["assigned_to"] == 7
        assert us_call.kwargs["tags"] == "urgent"
        assert us_call.kwargs["milestone"] == "null"

    def test_epic_not_found_raises(self, session, mock_client):
        mock_client.api.get.side_effect = [{"id": 1, "slug": "p", "name": "P"}, None]
        with pytest.raises(ValueError, match="Epic #7 not found"):
            wf.browse_backlog("p", epic=7, session_id=session)


class TestCreateTaskOptionalFields:
    def test_all_optional_fields_resolved(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(mock_client, get={"/userstories/by_ref": {"id": 50, "milestone": None}})

        def lr(resource, **kw):
            return {
                "milestones": [{"id": 9, "name": "S1"}],
                "task_statuses": [{"id": 3, "name": "In progress"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                    }
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        route(mock_client, post={"/tasks": {"id": 500, "ref": 80, "subject": "T"}})
        wf.create_task(
            "p",
            5,
            "T",
            description="d",
            status="In progress",
            assignee="bob",
            sprint="S1",
            due_date="2026-02-01",
            tags=["x"],
            blocked=True,
            session_id=session,
        )
        data = sent(mock_client, "post", "/tasks")[-1]
        assert data["description"] == "d"
        assert data["tags"] == ["x"]
        assert data["due_date"] == "2026-02-01"
        assert data["is_blocked"] is True
        assert data["status"] == 3
        assert data["assigned_to"] == 7
        assert data["milestone"] == 9


class TestUpdateTaskBranches:
    def _api_get(self):
        def api_get(path, params=None):
            if path == "/projects/by_slug":
                return {"id": 1, "slug": "p", "name": "P"}
            if path == "/tasks/by_ref":
                return {"id": 500, "version": 4}
            return None

        return api_get

    def test_resolves_all_fields(self, session, mock_client):
        route(
            mock_client,
            get={
                "/projects/by_slug": {"id": 1, "slug": "p", "name": "P"},
                "/tasks/by_ref": {"id": 500, "version": 4},
                "/userstories/by_ref": {"id": 60},
            },
            patch={
                "/tasks/500": {"ref": 80, "subject": "T2", "status_extra_info": {"name": "Done"}}
            },
        )

        def lr(resource, **kw):
            return {
                "task_statuses": [{"id": 3, "name": "Done"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                    }
                ],
                "milestones": [{"id": 9, "name": "S1"}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        wf.update_task(
            "p",
            80,
            subject="T2",
            description="d",
            status="Done",
            assignee="bob",
            sprint="S1",
            story_ref=12,
            blocked=True,
            tags=["x"],
            session_id=session,
        )
        (data,) = sent(mock_client, "patch", "/tasks/500")
        assert data["version"] == 4
        assert data["subject"] == "T2"
        assert data["status"] == 3
        assert data["assigned_to"] == 7
        assert data["milestone"] == 9
        assert data["user_story"] == 60
        assert data["is_blocked"] is True
        assert data["tags"] == ["x"]

    def test_no_fields_raises(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get()
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_task("p", 80, session_id=session)

    def test_task_not_found_raises(self, session, mock_client):
        def api_get(path, params=None):
            if path == "/projects/by_slug":
                return {"id": 1, "slug": "p", "name": "P"}
            return None  # /tasks/by_ref → not found

        mock_client.api.get.side_effect = api_get
        with pytest.raises(ValueError, match="Task #80 not found"):
            wf.update_task("p", 80, subject="x", session_id=session)


class TestCreateIssueAttributeResolution:
    def test_explicit_type_priority_severity_assignee(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}

        def lr(resource, **kw):
            return {
                "priorities": [{"id": 10, "name": "Low"}, {"id": 11, "name": "High"}],
                "severities": [{"id": 20, "name": "Minor"}, {"id": 21, "name": "Major"}],
                "issue_types": [{"id": 30, "name": "Bug"}, {"id": 31, "name": "Question"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                    }
                ],
                "issue_statuses": [{"id": 40, "name": "New"}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        route(mock_client, post={"/issues": {"id": 600, "ref": 90, "subject": "Bug"}})
        wf.create_issue(
            "p",
            "Bug",
            description="d",
            issue_type="Question",
            priority="High",
            severity="Major",
            assignee="bob",
            tags=["x"],
            session_id=session,
        )
        (data,) = sent(mock_client, "post", "/issues")
        assert data["project"] == 1
        assert data["subject"] == "Bug"
        assert data["type"] == 31
        assert data["priority"] == 11
        assert data["severity"] == 21
        assert data["assigned_to"] == 7
        assert data["tags"] == ["x"]
        assert data["description"] == "d"
        assert data["status"] == 40


class TestUpdateIssueBranches:
    def test_resolves_all_fields(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(
            mock_client,
            get={"/issues/by_ref": {"id": 600, "version": 2}},
            patch={"/issues/600": {"ref": 90, "subject": "B2"}},
        )

        def lr(resource, **kw):
            return {
                "issue_statuses": [{"id": 40, "name": "In progress"}],
                "priorities": [{"id": 11, "name": "High"}],
                "severities": [{"id": 21, "name": "Major"}],
                "issue_types": [{"id": 31, "name": "Question"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                    }
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        wf.update_issue(
            "p",
            90,
            subject="B2",
            description="d",
            status="In progress",
            assignee="bob",
            priority="High",
            severity="Major",
            issue_type="Question",
            blocked=True,
            tags=["x"],
            session_id=session,
        )
        (data,) = sent(mock_client, "patch", "/issues/600")
        assert data["version"] == 2
        assert data["status"] == 40
        assert data["assigned_to"] == 7
        assert data["priority"] == 11
        assert data["severity"] == 21
        assert data["type"] == 31
        assert data["is_blocked"] is True
        assert data["tags"] == ["x"]

    def test_issue_not_found_raises(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        route(mock_client, get={"/issues/by_ref": None})
        with pytest.raises(ValueError, match="Issue #90 not found"):
            wf.update_issue("p", 90, subject="x", session_id=session)


class TestSessionTools:
    def test_get_default_session_active(self, session, mock_client):
        # `session` fixture registers mock_client under DEFAULT_SESSION_ID too.
        result = wf.get_default_session()
        assert result["status"] == "active"
        assert result["auto_authenticated"] is True

    def test_get_default_session_unavailable(self):
        # No fixtures → active_sessions empty (cleared by autouse fixture).
        result = wf.get_default_session()
        assert result["status"] == "unavailable"

    def test_session_status_active(self, session, mock_client):
        route(mock_client, get={"/users/me": {"username": "bob"}})
        result = wf.session_status(session_id=session)
        assert result == {"status": "active", "session_id": session, "username": "bob"}

    def test_session_status_not_found(self):
        result = wf.session_status(session_id="ghost")
        assert result == {"status": "inactive", "reason": "not_found", "session_id": "ghost"}


class TestSingleItemGettersReturnDescription:
    """get_story / get_task / get_issue must surface the full `description` on a
    single-item read (the shared *_summary helpers omit it to keep board/list
    rows lean). Regression guard: the summary discarded description, forcing a
    REST-API detour to read it."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_get_story_includes_description(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 50,
                    "ref": 3,
                    "subject": "Story A",
                    "description": "The full story description.",
                    "status_extra_info": {"name": "New"},
                    "assigned_to_extra_info": {"full_name_display": "Alice"},
                    "tags": [],
                }
            },
        )
        result = wf.get_story("p", 3, session_id=session)
        assert result["description"] == "The full story description."
        # summary fields still present
        assert result["ref"] == 3
        assert result["subject"] == "Story A"

    def test_get_story_null_description_surfaced_as_none(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(mock_client, get={"/userstories/by_ref": {"id": 50, "ref": 3}})
        result = wf.get_story("p", 3, session_id=session)
        assert "description" in result
        assert result["description"] is None

    def test_get_task_includes_description(self, session, mock_client):
        task = {
            "id": 200,
            "ref": 7,
            "subject": "Task 1",
            "description": "Task detail.",
            "status_extra_info": {"name": "New"},
        }

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return self._project()

        mock_client.api.get.side_effect = api_get
        result = wf.get_task("p", 7, session_id=session)
        assert result["description"] == "Task detail."
        assert result["ref"] == 7

    def test_get_issue_includes_description(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(
            mock_client,
            get={
                "/issues/by_ref": {
                    "id": 300,
                    "ref": 5,
                    "subject": "Bug",
                    "description": "Issue detail.",
                    "status_extra_info": {"name": "New"},
                    "tags": [],
                }
            },
        )
        result = wf.get_issue("p", 5, session_id=session)
        assert result["description"] == "Issue detail."
        assert result["ref"] == 5

    def test_get_task_null_description_surfaced_as_none(self, session, mock_client):
        task = {"id": 200, "ref": 7}

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return self._project()

        mock_client.api.get.side_effect = api_get
        result = wf.get_task("p", 7, session_id=session)
        assert "description" in result
        assert result["description"] is None

    def test_get_issue_null_description_surfaced_as_none(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        route(mock_client, get={"/issues/by_ref": {"id": 300, "ref": 5}})
        result = wf.get_issue("p", 5, session_id=session)
        assert "description" in result
        assert result["description"] is None

    def test_get_epic_overview_includes_epic_description(self, session, mock_client):
        epic = {
            "id": 900,
            "ref": 12,
            "subject": "Epic X",
            "description": "Epic-level description.",
            "status_extra_info": {"name": "New"},
        }

        def api_get(path, params=None):
            if "/epics/by_ref" in str(path):
                return epic
            return self._project()

        mock_client.api.get.side_effect = api_get
        mock_client.list_resources.return_value = []
        result = wf.get_epic_overview("p", 12, session_id=session)
        assert result["epic"]["description"] == "Epic-level description."
        assert result["epic"]["ref"] == 12

    def test_summary_helpers_stay_lean(self):
        """The shared *_summary helpers must NOT gain description — they are reused
        by list/board composites (get_sprint_board reuses both story + task) where
        a full description per row would bloat responses."""
        blob = {"ref": 1, "subject": "X", "description": "big blob"}
        assert "description" not in wf._story_summary(blob)
        assert "description" not in wf._task_summary(blob)
        assert "description" not in wf._issue_summary(blob)


class TestLegacyServerMode:
    """v3 dropped the full server: TAIGA_SERVER_MODE no longer dispatches anything."""

    def test_full_mode_warns_and_does_not_raise(self, caplog):
        with caplog.at_level("WARNING", logger="src.server.app"):
            wf._warn_legacy_server_mode({"TAIGA_SERVER_MODE": "full"})
        assert "TAIGA_SERVER_MODE='full' is ignored" in caplog.text

    @pytest.mark.parametrize(
        "env", [{}, {"TAIGA_SERVER_MODE": "workflow"}, {"TAIGA_SERVER_MODE": ""}]
    )
    def test_unset_or_workflow_is_silent(self, caplog, env):
        with caplog.at_level("WARNING", logger="src.server.app"):
            wf._warn_legacy_server_mode(env)
        assert "TAIGA_SERVER_MODE" not in caplog.text


class TestThreadOffload:
    """v3 B4: tools run in worker threads so one slow Taiga call can't stall the loop."""

    def test_registered_tools_async_module_functions_sync(self):
        import inspect

        tools = wf.mcp._tool_manager.list_tools()
        assert tools
        for t in tools:
            assert t.is_async and inspect.iscoroutinefunction(t.fn), t.name
            # The module attribute stays the original sync function (tests call it).
            assert not inspect.iscoroutinefunction(getattr(wf, t.fn.__name__)), t.name

    def test_schemas_match_plain_registration(self):
        """functools.wraps must keep every schema exactly as FastMCP derives it from
        the original sync function."""
        from mcp.server.fastmcp import FastMCP

        plain = FastMCP("plain")
        for t in wf.mcp._tool_manager.list_tools():
            original = t.fn.__wrapped__
            plain.add_tool(original, name=t.name, description=t.description)
            ref = plain._tool_manager.get_tool(t.name)
            assert t.parameters == ref.parameters, t.name
            assert t.output_schema == ref.output_schema, t.name

    def test_two_slow_calls_overlap(self, session, mock_client):
        import time as _time

        def slow_me(path, **kwargs):
            _time.sleep(0.5)  # a slow Taiga round trip
            return {"id": 1, "username": "jdoe", "email": "jdoe@x"}

        mock_client.api.get.side_effect = slow_me

        async def scenario():
            ticks = 0

            async def heartbeat():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            hb = asyncio.create_task(heartbeat())
            start = _time.perf_counter()
            await asyncio.gather(
                wf.mcp.call_tool("get_current_user", {"session_id": session}),
                wf.mcp.call_tool("get_current_user", {"session_id": session}),
            )
            elapsed = _time.perf_counter() - start
            hb.cancel()
            return elapsed, ticks

        elapsed, ticks = asyncio.run(scenario())
        # Serial would be >= 1.0 s; the margin absorbs thread start-up on slow runners.
        assert elapsed < 0.9, f"calls ran serially ({elapsed:.2f}s)"
        # The event loop kept running while both calls slept (≈25 ticks expected).
        assert ticks >= 5, f"event loop was blocked (only {ticks} heartbeats)"

    def test_control_plain_sync_tool_blocks_the_loop(self):
        """The problem B4 fixes: FastMCP runs a plain sync tool on the event loop.

        If a future mcp release offloads sync tools itself, this fails: that's the
        signal app.tool's offload has become redundant, not that something broke here.
        """
        import time as _time

        from mcp.server.fastmcp import FastMCP

        plain = FastMCP("plain")

        @plain.tool("slow")
        def slow() -> str:
            _time.sleep(0.5)
            return "ok"

        async def scenario():
            start = _time.perf_counter()
            await asyncio.gather(plain.call_tool("slow", {}), plain.call_tool("slow", {}))
            return _time.perf_counter() - start

        assert asyncio.run(scenario()) >= 0.95


class TestCachedAcrossLogout:
    def test_logout_during_compute_does_not_resurrect_cache(self, session, mock_client):
        """A tool thread computing a cache entry while another thread logs the
        session out must not recreate the cache for the next login to inherit."""

        def factory():
            wf.logout(session_id=wf.DEFAULT_SESSION_ID)  # concurrent logout, mid-compute
            return ["stale members"]

        assert wf._cached(wf.DEFAULT_SESSION_ID, "members_1", factory) == ["stale members"]
        assert wf.DEFAULT_SESSION_ID not in wf._session_cache or (
            "members_1" not in wf._session_cache[wf.DEFAULT_SESSION_ID]
        )

    def test_normal_compute_is_cached(self, session, mock_client):
        calls = []

        def factory():
            calls.append(1)
            return ["members"]

        wf._cached(session, "members_1", factory)
        wf._cached(session, "members_1", factory)
        assert calls == [1]


# ---------------------------------------------------------------------------
# v3 B5: list_items, update_epic, delete_item (GitHub #144)
# ---------------------------------------------------------------------------

PROJECT = {"id": 1, "slug": "p", "name": "P"}


class TestListItems:
    def test_unknown_epic_filter_is_value_error(self, session, mock_client):
        route(
            mock_client,
            get={
                "/projects/by_slug": PROJECT,
                "/epics/by_ref": _api_error(TaigaNotFoundError, 404, "No Epic matches"),
            },
        )
        with pytest.raises(ValueError, match="Epic #4 not found"):
            wf.list_items("p", epic=4, session_id=session)

    def test_stories_include_sprint_assigned_and_no_backlog_filter(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        mock_client.list_resources.return_value = [
            {"ref": 1, "subject": "in sprint", "milestone_name": "S1"},
            {"ref": 2, "subject": "backlog"},
        ]
        result = wf.list_items("p", session_id=session)
        assert [s["ref"] for s in result] == [1, 2]
        # No milestone filter at all: browse_backlog's "milestone": "null" must not leak in.
        mock_client.list_resources.assert_called_once_with("user_stories", project_id=1)

    def test_story_filters_resolved(self, session, mock_client):
        route(mock_client, get={"/projects/by_slug": PROJECT, "/epics/by_ref": {"id": 50}})

        def lr(resource, project_id=None, **filters):
            return {
                "userstory_statuses": [{"id": 3, "name": "Done"}],
                "memberships": [{"user": 7, "full_name": "Bob", "email": "bob@x"}],
                "milestones": [{"id": 9, "name": "S1"}],
                "user_stories": [],
            }[resource]

        mock_client.list_resources.side_effect = lr
        wf.list_items(
            "p",
            status="Done",
            sprint="S1",
            epic=4,
            tags="a,b",
            assignee="bob",
            session_id=session,
        )
        last = mock_client.list_resources.call_args
        assert last.args == ("user_stories",)
        assert last.kwargs == {
            "project_id": 1,
            "status": 3,
            "assigned_to": 7,
            "milestone": 9,
            "epic": 50,
            "tags": "a,b",
        }

    def test_epics_with_status_filter(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT

        def lr(resource, project_id=None, **filters):
            if resource == "epic_statuses":
                return [{"id": 2, "name": "In progress"}]
            assert resource == "epics" and filters == {"status": 2}
            return [{"ref": 4, "subject": "E", "status_extra_info": {"name": "In progress"}}]

        mock_client.list_resources.side_effect = lr
        result = wf.list_items("p", entity_type="epic", status="In progress", session_id=session)
        assert result[0]["ref"] == 4 and result[0]["status"] == "In progress"

    @pytest.mark.parametrize("status,closed", [("open", False), ("Closed", True)])
    def test_sprints_by_open_or_closed(self, session, mock_client, status, closed):
        mock_client.api.get.return_value = PROJECT
        mock_client.list_resources.return_value = [{"id": 9, "name": "S1", "closed": closed}]
        result = wf.list_items("p", entity_type="sprint", status=status, session_id=session)
        mock_client.list_resources.assert_called_once_with(
            "milestones", project_id=1, closed=closed
        )
        assert result[0]["name"] == "S1" and result[0]["is_closed"] is closed

    def test_bad_sprint_status_raises(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        with pytest.raises(ValueError, match="'open' or 'closed'"):
            wf.list_items("p", entity_type="sprint", status="done", session_id=session)

    @pytest.mark.parametrize(
        "entity_type,kwargs",
        [("epic", {"sprint": "S1"}), ("epic", {"tags": "x"}), ("sprint", {"assignee": "bob"})],
    )
    def test_filter_not_supported_by_type_is_rejected_before_any_call(
        self, session, mock_client, entity_type, kwargs
    ):
        # Taiga silently ignores unknown query params: passing one through would
        # return the UNFILTERED list. Reject it instead.
        with pytest.raises(ValueError, match="do not apply"):
            wf.list_items("p", entity_type=entity_type, session_id=session, **kwargs)
        mock_client.api.get.assert_not_called()
        mock_client.list_resources.assert_not_called()

    def test_unknown_entity_type(self, session, mock_client):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            wf.list_items("p", entity_type="issue", session_id=session)


class TestUpdateEpic:
    def _route(self, mock_client, patched=None):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/epics/by_ref": {"id": 500, "ref": 4, "version": 6}},
            patch={"/epics/500": patched or {"ref": 4, "subject": "New"}},
        )

    def test_patches_only_given_fields_with_version(self, session, mock_client):
        self._route(mock_client)

        def lr(resource, project_id=None, **kw):
            return {
                "epic_statuses": [{"id": 2, "name": "Done"}],
                "memberships": [{"user": 7, "full_name": "Bob", "email": "bob@x"}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        result = wf.update_epic(
            "p",
            4,
            subject="New",
            status="Done",
            assignee="bob",
            color="#A5694F",
            tags=["x"],
            blocked=False,
            session_id=session,
        )
        assert sent(mock_client, "patch", "/epics/500") == [
            {
                "version": 6,
                "subject": "New",
                "status": 2,
                "assigned_to": 7,
                "color": "#A5694F",
                "tags": ["x"],
                "is_blocked": False,
            }
        ]
        assert result["subject"] == "New"

    def test_no_fields_raises_before_any_call(self, session, mock_client):
        self._route(mock_client)
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_epic("p", 4, session_id=session)
        mock_client.api.get.assert_not_called()
        mock_client.api.patch.assert_not_called()

    def test_unknown_epic(self, session, mock_client):
        # Real Taiga answers an unknown ref with a 404, never an empty body.
        route(
            mock_client,
            get={
                "/projects/by_slug": PROJECT,
                "/epics/by_ref": _api_error(TaigaNotFoundError, 404, "No Epic matches"),
            },
        )
        with pytest.raises(ValueError, match="Epic #4 not found"):
            wf.update_epic("p", 4, subject="x", session_id=session)


class TestDescriptionEdits:
    """Partial description edits on the four update tools (GitHub #194)."""

    TOOLS = [
        (wf.update_epic, "epics"),
        (wf.update_story, "userstories"),
        (wf.update_issue, "issues"),
        (wf.update_task, "tasks"),
    ]
    TEXT = "# Plan\n| a | 1 |\n|--|--|--|\n| b | 2 |\n## History\n- v51\n"

    def _route(self, mock_client, segment, patch=None, text=TEXT):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={f"/{segment}/by_ref": {"id": 500, "ref": 4, "version": 6, "description": text}},
            patch={
                f"/{segment}/500": patch
                if patch is not None
                else (lambda json: {**json, "ref": 4, "version": 7})
            },
        )

    @pytest.mark.parametrize("tool,segment", TOOLS)
    def test_replace_and_append_in_one_patch(self, session, mock_client, tool, segment):
        self._route(mock_client, segment)
        result = tool(
            "p",
            4,
            description_edits=[{"old": "| b | 2 |", "new": "| b | 3 |"}],
            description_append="- v52\n",
            session_id=session,
        )
        expected = self.TEXT.replace("| b | 2 |", "| b | 3 |") + "- v52\n"
        assert sent(mock_client, "patch", f"/{segment}/500") == [
            {"version": 6, "description": expected}
        ]
        assert result["description_change"] == {
            "replacements": 1,
            "prepended": False,
            "appended": True,
            "length_before": len(self.TEXT),
            "length_after": len(expected),
            "version": 7,
        }

    @pytest.mark.parametrize("tool,segment", TOOLS)
    @pytest.mark.parametrize(
        "old,match",
        [("| c | 9 |", "not found"), ("| ", "found 4 times"), ("--|--", "found 2 times")],
    )
    def test_absent_or_repeated_old_writes_nothing(
        self, session, mock_client, tool, segment, old, match
    ):
        self._route(mock_client, segment)
        with pytest.raises(ValueError, match=match):
            tool("p", 4, description_edits=[{"old": old, "new": "x"}], session_id=session)
        mock_client.api.patch.assert_not_called()

    def test_edits_apply_in_order_then_prepend(self, session, mock_client):
        self._route(mock_client, "epics")
        wf.update_epic(
            "p",
            4,
            description_edits=[
                {"old": "| a | 1 |", "new": "| a | X |"},
                # Only unique once the first edit has run.
                {"old": "X", "new": "Y"},
            ],
            description_prepend="> draft\n",
            session_id=session,
        )
        (payload,) = sent(mock_client, "patch", "/epics/500")
        assert payload["description"] == "> draft\n" + self.TEXT.replace("| a | 1 |", "| a | Y |")

    def test_empty_description_is_edited_as_empty_text(self, session, mock_client):
        self._route(mock_client, "userstories", text=None)
        wf.update_story("p", 4, description_append="first line", session_id=session)
        assert sent(mock_client, "patch", "/userstories/500") == [
            {"version": 6, "description": "first line"}
        ]

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"description": "x", "description_append": "y"}, "do not combine"),
            ({"description_edits": [{"old": "", "new": "x"}]}, "old is empty"),
            ({"description_edits": [{"old": "a"}]}, "exactly two string keys"),
            ({"description_edits": [{"old": "a", "new": 1}]}, "exactly two string keys"),
        ],
    )
    def test_bad_arguments_raise_before_any_call(self, session, mock_client, kwargs, match):
        self._route(mock_client, "epics")
        with pytest.raises(ValueError, match=match):
            wf.update_epic("p", 4, session_id=session, **kwargs)
        mock_client.api.get.assert_not_called()
        mock_client.api.patch.assert_not_called()

    def test_empty_edits_list_counts_as_no_field(self, session, mock_client):
        self._route(mock_client, "epics")
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_epic("p", 4, description_edits=[], session_id=session)

    def test_whole_text_replacement_reports_lengths(self, session, mock_client):
        self._route(mock_client, "tasks")
        result = wf.update_task("p", 4, description="short", session_id=session)
        assert result["description_change"] == {
            "replaced_whole_text": True,
            "length_before": len(self.TEXT),
            "length_after": 5,
            "version": 7,
        }

    def test_no_description_change_no_report(self, session, mock_client):
        self._route(mock_client, "tasks")
        result = wf.update_task("p", 4, subject="New", session_id=session)
        assert "description_change" not in result

    @pytest.mark.parametrize("tool,segment", TOOLS)
    def test_callers_version_is_sent_for_taiga_to_check(self, session, mock_client, tool, segment):
        # Taiga's check is field-aware: an older version still goes through when
        # only other fields (or comments) changed since, so it is Taiga's call.
        self._route(mock_client, segment)
        tool("p", 4, description_append="x", version=5, session_id=session)
        assert sent(mock_client, "patch", f"/{segment}/500") == [
            {"version": 5, "description": self.TEXT + "x"}
        ]

    def test_matching_version_is_sent(self, session, mock_client):
        self._route(mock_client, "issues")
        wf.update_issue("p", 4, description_append="x", version=6, session_id=session)
        assert sent(mock_client, "patch", "/issues/500") == [
            {"version": 6, "description": self.TEXT + "x"}
        ]

    @pytest.mark.parametrize("tool,segment", TOOLS)
    def test_taiga_version_mismatch_is_a_conflict_not_retried(
        self, session, mock_client, tool, segment
    ):
        # Taiga answers a stale version with 400 {"version": ...}, raised as
        # TaigaConcurrencyError by the client (see test_taiga_client).
        error = TaigaConcurrencyError(
            400,
            "version: The version doesn't match with the current one",
            {"version": "The version doesn't match with the current one"},
        )
        self._route(mock_client, segment, patch=error)
        with pytest.raises(ValueError, match="Version conflict: a field this update writes"):
            tool("p", 4, description_append="x", session_id=session)
        assert mock_client.api.patch.call_count == 1

    def test_other_bad_request_is_not_a_conflict(self, session, mock_client):
        self._route(mock_client, "epics", patch=_api_error(TaigaBadRequestError, 400, "bad"))
        with pytest.raises(TaigaBadRequestError):
            wf.update_epic("p", 4, description_append="x", session_id=session)

    def _route_story_relink(self, mock_client, post):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": {
                    "id": 500,
                    "ref": 4,
                    "version": 6,
                    "description": "d",
                    "epics": [{"id": 70}],
                },
                "/epics/by_ref": {"id": 80, "ref": 9},
            },
            patch={"/userstories/500": lambda json: {**json, "ref": 4, "version": 7}},
            delete={"/epics/70/related_userstories/500": None},
            post={"/epics/80/related_userstories": post},
        )

    def test_story_relink_failure_after_saved_patch_is_reported(self, session, mock_client):
        self._route_story_relink(mock_client, _api_error(TaigaServerError, 500, "down"))
        result = wf.update_story("p", 4, description_append="x", epic=9, session_id=session)
        assert sent(mock_client, "patch", "/userstories/500") == [
            {"version": 6, "description": "dx"}
        ]
        assert result["description_change"]["version"] == 7
        assert "relinking to epic #9 failed" in result["epic_error"]
        assert "epic" not in result
        assert result["epics"] is None

    def test_story_relink_failure_alone_still_raises(self, session, mock_client):
        self._route_story_relink(mock_client, _api_error(TaigaServerError, 500, "down"))
        with pytest.raises(TaigaServerError):
            wf.update_story("p", 4, epic=9, session_id=session)
        mock_client.api.patch.assert_not_called()

    def test_story_conflict_leaves_epic_links_alone(self, session, mock_client):
        self._route_story_relink(mock_client, {})
        route(
            mock_client,
            patch={
                "/userstories/500": TaigaConcurrencyError(400, "version: stale", {"version": "x"})
            },
        )
        with pytest.raises(ValueError, match="Version conflict"):
            wf.update_story("p", 4, description_append="x", epic=9, session_id=session)
        mock_client.api.delete.assert_not_called()
        mock_client.api.post.assert_not_called()

    @pytest.mark.parametrize(
        "tool,segment,key",
        [
            (wf.get_story, "userstories", None),
            (wf.get_task, "tasks", None),
            (wf.get_issue, "issues", None),
            (wf.get_epic_overview, "epics", "epic"),
        ],
    )
    def test_single_reads_return_version(self, session, mock_client, tool, segment, key):
        mock_client.api.get.return_value = PROJECT
        mock_client.list_resources.return_value = []
        route(mock_client, get={f"/{segment}/by_ref": {"id": 500, "ref": 4, "version": 6}})
        result = tool("p", 4, session_id=session)
        assert (result[key] if key else result)["version"] == 6


class TestDeleteItem:
    @pytest.mark.parametrize(
        "entity_type,segment",
        [("story", "userstories"), ("task", "tasks"), ("issue", "issues"), ("epic", "epics")],
    )
    def test_by_ref_resolves_then_deletes_and_reports_subject(
        self, session, mock_client, entity_type, segment
    ):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={f"/{segment}/by_ref": {"id": 77, "ref": 5, "subject": "Doomed"}},
            delete={f"/{segment}/77": None},
        )
        result = wf.delete_item("p", entity_type, 5, session_id=session)
        assert result == {
            "status": "deleted",
            "entity_type": entity_type,
            "id": 77,
            "ref": 5,
            "subject": "Doomed",
        }
        mock_client.api.delete.assert_called_once_with(f"/{segment}/77")

    def test_sprint_by_name(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        mock_client.list_resources.return_value = [{"id": 9, "name": "S1"}]
        route(mock_client, delete={"/milestones/9": None})
        result = wf.delete_item("p", "sprint", "S1", session_id=session)
        assert result == {"status": "deleted", "entity_type": "sprint", "id": 9, "name": "S1"}

    def test_wiki_by_slug(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/wiki/by_slug": {"id": 3, "slug": "home"}},
            delete={"/wiki/3": None},
        )
        result = wf.delete_item("p", "wiki", "home", session_id=session)
        assert result == {"status": "deleted", "entity_type": "wiki", "id": 3, "slug": "home"}

    def test_wiki_404_is_not_found_not_a_crash(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/wiki/by_slug": _api_error(TaigaNotFoundError, 404, "nope")})
        with pytest.raises(ValueError, match="Wiki page 'gone' not found"):
            wf.delete_item("p", "wiki", "gone", session_id=session)
        mock_client.api.delete.assert_not_called()

    def test_real_404_for_unknown_ref_is_a_clear_not_found(self, session, mock_client):
        # Real Taiga: /userstories/by_ref answers an unknown ref with 404, not an empty body.
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": _api_error(TaigaNotFoundError, 404, "No match")},
        )
        with pytest.raises(ValueError, match="Story #5 not found"):
            wf.delete_item("p", "story", 5, session_id=session)
        mock_client.api.delete.assert_not_called()

    def test_unknown_ref_deletes_nothing(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/userstories/by_ref": None})
        with pytest.raises(ValueError, match="Story #5 not found"):
            wf.delete_item("p", "story", 5, session_id=session)
        mock_client.api.delete.assert_not_called()

    @pytest.mark.parametrize("bad", ["S1", True, "5a"])
    def test_ref_types_require_a_ref_number(self, session, mock_client, bad):
        with pytest.raises(ValueError, match="ref number"):
            wf.delete_item("p", "story", bad, session_id=session)
        mock_client.api.delete.assert_not_called()

    def test_numeric_string_ref_accepted(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/tasks/by_ref": {"id": 8, "ref": 12, "subject": "T"}},
            delete={"/tasks/8": None},
        )
        assert wf.delete_item("p", "task", "12", session_id=session)["ref"] == 12

    def test_description_states_cascades(self):
        tool = wf.mcp._tool_manager.get_tool("delete_item")
        for fact in ("PERMANENTLY", "ALSO DELETES ITS TASKS", "keeps its stories"):
            assert fact in tool.description


class TestBrowseBacklogDescription:
    def test_says_backlog_only_and_points_to_list_items(self):
        # #144: the old wording read as "the project's stories" and hid sprint-assigned ones.
        desc = wf.mcp._tool_manager.get_tool("browse_backlog").description
        assert "ONLY the user stories assigned to no sprint" in desc
        assert "list_items" in desc


# ---------------------------------------------------------------------------
# v3 B6: comments & history (GitHub #154). Shapes taken from real Taiga.
# ---------------------------------------------------------------------------


def _hist(id_, created, comment="", diff=None, edited=None, deleted=None, name="Jane"):
    return {
        "id": id_,
        "created_at": created,
        "comment": comment,
        "diff": diff or {},
        "edit_comment_date": edited,
        "delete_comment_date": deleted,
        "user": {"pk": 6, "username": "jdoe", "name": name},
    }


STORY = {"id": 50, "ref": 3, "version": 2}
# Taiga returns history newest first.
HISTORY = [
    _hist("c3", "2026-10-03T00:00:00Z", "deleted one", deleted="2026-10-04T00:00:00Z"),
    _hist("h2", "2026-10-02T00:00:00Z", diff={"description": 1, "description_html": 1}),
    _hist("c1", "2026-10-01T00:00:00Z", "first\n" + "x" * 400, edited="2026-10-05T00:00:00Z"),
]


class TestListComments:
    def _route(self, mock_client, history=HISTORY):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY, "/history/userstory/50": history},
        )

    def test_full_text_oldest_first_deleted_hidden(self, session, mock_client):
        self._route(mock_client)
        result = wf.list_comments("p", 3, session_id=session)
        assert [c["id"] for c in result] == ["c1"]
        assert result[0]["comment"] == "first\n" + "x" * 400  # never truncated (#154)
        assert result[0]["author"] == "Jane"
        assert result[0]["edited_at"] == "2026-10-05T00:00:00Z"

    def test_include_deleted(self, session, mock_client):
        self._route(mock_client)
        result = wf.list_comments("p", 3, include_deleted=True, session_id=session)
        assert [c["id"] for c in result] == ["c1", "c3"]
        assert result[1]["deleted_at"] == "2026-10-04T00:00:00Z"

    @pytest.mark.parametrize(
        "entity_type,segment,hist",
        [("task", "tasks", "task"), ("issue", "issues", "issue"), ("epic", "epics", "epic")],
    )
    def test_history_segment_is_the_singular_model_name(
        self, session, mock_client, entity_type, segment, hist
    ):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={f"/{segment}/by_ref": {"id": 9}, f"/history/{hist}/9": []})
        assert wf.list_comments("p", 1, entity_type=entity_type, session_id=session) == []

    def test_unknown_ref_real_404(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/userstories/by_ref": _api_error(TaigaNotFoundError, 404)})
        with pytest.raises(ValueError, match="Story #3 not found"):
            wf.list_comments("p", 3, session_id=session)


class TestCommentActions:
    @pytest.mark.parametrize(
        "tool,action,extra,status",
        [
            ("delete_comment", "delete_comment", {}, "comment_deleted"),
            ("restore_comment", "undelete_comment", {}, "comment_restored"),
        ],
    )
    def test_delete_and_restore_post_with_id_query(
        self, session, mock_client, tool, action, extra, status
    ):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY},
            post={f"/history/userstory/50/{action}": None},
        )
        result = getattr(wf, tool)("p", 3, "c1", session_id=session)
        assert result["status"] == status
        mock_client.api.post.assert_called_once_with(
            f"/history/userstory/50/{action}", params={"id": "c1"}, json=None
        )

    def test_edit_sends_text_in_body_id_in_query(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY},
            post={"/history/userstory/50/edit_comment": None},
        )
        wf.edit_comment("p", 3, "c1", "line1\\nline2", session_id=session)
        mock_client.api.post.assert_called_once_with(
            "/history/userstory/50/edit_comment",
            params={"id": "c1"},
            json={"comment": "line1\nline2"},  # literal \\n from MCP JSON unescaped
        )

    def test_unknown_comment_id_is_clear(self, session, mock_client):
        # Real Taiga: a wrong comment id is a 404 with an empty body.
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY},
            post={"/history/userstory/50/delete_comment": _api_error(TaigaNotFoundError, 404, "")},
        )
        with pytest.raises(ValueError, match="Comment nope not found on story #3"):
            wf.delete_comment("p", 3, "nope", session_id=session)

    @pytest.mark.parametrize("tool", ["edit_comment", "delete_comment", "restore_comment"])
    def test_empty_comment_id_rejected_before_any_call(self, session, mock_client, tool):
        args = ("p", 3, "  ", "text") if tool == "edit_comment" else ("p", 3, "  ")
        with pytest.raises(ValueError, match="comment_id must not be empty"):
            getattr(wf, tool)(*args, session_id=session)
        mock_client.api.get.assert_not_called()

    def test_edit_empty_text_rejected(self, session, mock_client):
        with pytest.raises(ValueError, match="must not be empty"):
            wf.edit_comment("p", 3, "c1", "   ", session_id=session)
        mock_client.api.post.assert_not_called()


class TestGetHistory:
    def test_entries_newest_first_with_changed_fields(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/userstories/by_ref": STORY, "/history/userstory/50": HISTORY})
        result = wf.get_history("p", 3, session_id=session)
        assert [e["id"] for e in result] == ["c3", "h2", "c1"]
        assert result[1]["changed_fields"] == ["description"]  # *_html duplicates dropped
        assert result[1].get("comment") is None
        assert result[0]["comment_deleted_at"] == "2026-10-04T00:00:00Z"
        assert "comment_versions" not in result[2]

    def test_comment_versions_only_for_edited_comments(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/history/userstory/50": HISTORY,
                "/history/userstory/50/comment_versions": [
                    {
                        "date": "2026-10-01T00:00:00Z",
                        "user": {"username": "jdoe", "full_name_display": "Jane"},
                        "comment": "original",
                    }
                ],
            },
        )
        result = wf.get_history("p", 3, include_comment_versions=True, session_id=session)
        versions_calls = [
            c for c in mock_client.api.get.call_args_list if c.args[0].endswith("comment_versions")
        ]
        assert len(versions_calls) == 1  # only c1 was edited
        assert versions_calls[0].kwargs["params"] == {"id": "c1"}
        assert result[2]["comment_versions"] == [
            {"date": "2026-10-01T00:00:00Z", "author": "Jane", "comment": "original"}
        ]

    def test_history_requested_unpaginated(self, session, mock_client):
        # /history pages at 30 entries; a long history must not lose its oldest comments.
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/userstories/by_ref": STORY, "/history/userstory/50": HISTORY})
        wf.get_history("p", 3, session_id=session)
        wf.list_comments("p", 3, session_id=session)
        hist_calls = [
            c for c in mock_client.api.get.call_args_list if c.args[0] == "/history/userstory/50"
        ]
        assert len(hist_calls) == 2
        assert all(c.kwargs["headers"] == {"x-disable-pagination": "True"} for c in hist_calls)

    def test_forbidden_comment_versions_do_not_abort_history(self, session, mock_client):
        # Versions are visible only to the comment author and project admins.
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/history/userstory/50": HISTORY,
                "/history/userstory/50/comment_versions": TaigaAuthenticationError(403, "no"),
            },
        )
        result = wf.get_history("p", 3, include_comment_versions=True, session_id=session)
        assert [e["id"] for e in result] == ["c3", "h2", "c1"]
        assert result[2]["comment_versions"] is None
        assert "forbidden" in result[2]["comment_versions_error"]


class TestActivityComments:
    def _event(self, comment):
        return {"event_type": "userstories.userstory.change", "data": {"comment": comment}}

    def test_long_comment_truncated_visibly(self):
        s = wf._summarize_timeline_event(self._event("y" * 500))
        assert s["comment"] == "y" * 200 and s["comment_truncated"] is True

    def test_full_comments_flag_keeps_everything(self):
        s = wf._summarize_timeline_event(self._event("y" * 500), full_comments=True)
        assert s["comment"] == "y" * 500 and "comment_truncated" not in s

    def test_short_comment_untouched(self):
        s = wf._summarize_timeline_event(self._event("short"))
        assert s["comment"] == "short" and "comment_truncated" not in s


# ---------------------------------------------------------------------------
# v3 B7: attachments & tags. Shapes taken from real Taiga.
# ---------------------------------------------------------------------------

import base64 as _b64  # noqa: E402

ATTACHMENT = {
    "id": 183,
    "name": "spec.txt",
    "size": 5,
    "description": "d",
    "url": "https://taiga.example/media/attachments/x/spec.txt",
    "is_deprecated": False,
    "object_id": 50,
    "created_date": "2026-10-22T13:11:07Z",
}


class TestAttachments:
    def test_list_unpaginated_by_object_and_project(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY, "/userstories/attachments": [ATTACHMENT]},
        )
        result = wf.list_attachments("p", 3, session_id=session)
        assert result == [
            {
                "id": 183,
                "name": "spec.txt",
                "size": 5,
                "description": "d",
                "url": ATTACHMENT["url"],
                "is_deprecated": False,
                "created_at": "2026-10-22T13:11:07Z",
            }
        ]
        mock_client.api.get.assert_any_call(
            "/userstories/attachments",
            params={"object_id": 50, "project": 1},
            headers={"x-disable-pagination": "True"},
        )

    def test_add_decodes_base64_and_uploads_multipart(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/tasks/by_ref": {"id": 77}},
            post={"/tasks/attachments": {**ATTACHMENT, "object_id": 77}},
        )
        content = _b64.b64encode(b"hello").decode()
        result = wf.add_attachment(
            "p",
            9,
            "../../etc/spec.txt",
            content,
            description="d",
            entity_type="task",
            session_id=session,
        )
        assert result["status"] == "attached" and result["id"] == 183
        mock_client.api.post.assert_called_once_with(
            "/tasks/attachments",
            data={"project": "1", "object_id": "77", "description": "d"},
            files={"attached_file": ("spec.txt", b"hello", "text/plain")},  # basename only
        )

    @pytest.mark.parametrize(
        "filename,content,match",
        [
            ("", "aGVsbG8=", "filename must not be empty"),
            ("a.txt", "not base64!!", "not valid base64"),
            ("a.txt", "", "empty file"),
        ],
    )
    def test_add_rejects_bad_input_before_any_call(
        self, session, mock_client, filename, content, match
    ):
        with pytest.raises(ValueError, match=match):
            wf.add_attachment("p", 3, filename, content, session_id=session)
        mock_client.api.get.assert_not_called()

    def test_add_rejects_over_10mb(self, session, mock_client, monkeypatch):
        from src.server.tools import attachments as att

        monkeypatch.setattr(att, "_MAX_ATTACHMENT_BYTES", 4)
        with pytest.raises(ValueError, match="limit"):
            wf.add_attachment(
                "p", 3, "a.txt", _b64.b64encode(b"hello").decode(), session_id=session
            )
        mock_client.api.post.assert_not_called()

    def test_delete_checks_ownership_then_deletes(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY, "/userstories/attachments/183": ATTACHMENT},
            delete={"/userstories/attachments/183": None},
        )
        result = wf.delete_attachment("p", 3, 183, session_id=session)
        assert result["status"] == "deleted" and result["name"] == "spec.txt"
        mock_client.api.delete.assert_called_once_with("/userstories/attachments/183")

    def test_delete_refuses_attachment_of_another_item(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/userstories/attachments/183": {**ATTACHMENT, "object_id": 999},
            },
        )
        with pytest.raises(ValueError, match="not attached to story #3"):
            wf.delete_attachment("p", 3, 183, session_id=session)
        mock_client.api.delete.assert_not_called()

    def test_delete_unknown_attachment_real_404(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/userstories/attachments/5": _api_error(TaigaNotFoundError, 404),
            },
        )
        with pytest.raises(ValueError, match="not attached"):
            wf.delete_attachment("p", 3, 5, session_id=session)
        mock_client.api.delete.assert_not_called()


class TestTags:
    def test_list_sorted_with_colors(self, session, mock_client):
        route(
            mock_client,
            get={"/projects/by_slug": PROJECT, "/projects/1/tags_colors": {"b": None, "a": "#fff"}},
        )
        assert wf.list_tags("p", session_id=session) == [
            {"tag": "a", "color": "#fff"},
            {"tag": "b", "color": None},
        ]

    @pytest.mark.parametrize(
        "kwargs,payload",
        [
            ({"new_name": "y"}, {"from_tag": "x", "to_tag": "y"}),
            ({"color": "#A5694F"}, {"from_tag": "x", "color": "#A5694F"}),
            ({"new_name": "y", "color": "#000"}, {"from_tag": "x", "to_tag": "y", "color": "#000"}),
        ],
    )
    def test_rename_uses_from_tag_to_tag(self, session, mock_client, kwargs, payload):
        # Real Taiga's EditTagTagValidator: from_tag/to_tag/color (the pre-v3
        # full server sent tag/new_tag, which Taiga does not read).
        mock_client.api.get.return_value = PROJECT
        route(mock_client, post={"/projects/1/edit_tag": None})
        wf.rename_tag("p", "x", session_id=session, **kwargs)
        assert sent(mock_client, "post", "/projects/1/edit_tag") == [payload]

    def test_rename_needs_a_change(self, session, mock_client):
        with pytest.raises(ValueError, match="new_name, color, or both"):
            wf.rename_tag("p", "x", session_id=session)
        mock_client.api.get.assert_not_called()

    def test_merge(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, post={"/projects/1/mix_tags": None})
        result = wf.merge_tags("p", [" a ", "b", ""], "c", session_id=session)
        assert result == {"status": "tags_merged", "from_tags": ["a", "b"], "to_tag": "c"}
        assert sent(mock_client, "post", "/projects/1/mix_tags") == [
            {"from_tags": ["a", "b"], "to_tag": "c"}
        ]

    @pytest.mark.parametrize(
        "from_tags,to_tag,match",
        [
            ([], "c", "at least one"),
            (["a"], "", "to_tag must not be empty"),
            (["a"], "a", "not be one of"),
        ],
    )
    def test_merge_validation(self, session, mock_client, from_tags, to_tag, match):
        with pytest.raises(ValueError, match=match):
            wf.merge_tags("p", from_tags, to_tag, session_id=session)
        mock_client.api.get.assert_not_called()

    @pytest.mark.parametrize(
        "tool,args", [("rename_tag", ("p", "x", "y")), ("merge_tags", ("p", ["a"], "c"))]
    )
    def test_403_says_project_admin_required(self, session, mock_client, tool, args):
        from src.taiga_client import TaigaAuthenticationError

        mock_client.api.get.return_value = PROJECT
        denied = TaigaAuthenticationError(
            403, "You do not have permission to perform this action.", {}
        )
        path = "/projects/1/edit_tag" if tool == "rename_tag" else "/projects/1/mix_tags"
        route(mock_client, post={path: denied})
        with pytest.raises(
            TaigaAuthenticationError, match=f"{tool} requires project admin rights on 'p'"
        ):
            getattr(wf, tool)(*args, session_id=session)


class TestB7ReviewFixes:
    def _route_upload(self, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={"/userstories/by_ref": STORY},
            post={"/userstories/attachments": ATTACHMENT},
        )

    def test_line_wrapped_base64_accepted(self, session, mock_client):
        # `base64` CLI / base64.encodebytes wrap at 76 columns.
        self._route_upload(mock_client)
        data = b"x" * 200
        wf.add_attachment("p", 3, "a.bin", _b64.encodebytes(data).decode(), session_id=session)
        assert mock_client.api.post.call_args.kwargs["files"]["attached_file"][1] == data

    def test_windows_path_reduced_to_file_name(self, session, mock_client):
        self._route_upload(mock_client)
        wf.add_attachment(
            "p", 3, "C:\\Users\\me\\spec.pdf", _b64.b64encode(b"%PDF").decode(), session_id=session
        )
        name, _, mime = mock_client.api.post.call_args.kwargs["files"]["attached_file"]
        assert name == "spec.pdf" and mime == "application/pdf"

    def test_oversize_rejected_before_decoding(self, session, mock_client, monkeypatch):
        from src.server.tools import attachments as att

        monkeypatch.setattr(att, "_MAX_ATTACHMENT_BYTES", 3)
        decoded = []
        monkeypatch.setattr(att.base64, "b64decode", lambda *a, **k: decoded.append(1) or b"")
        with pytest.raises(ValueError, match="limit"):
            wf.add_attachment("p", 3, "a", _b64.b64encode(b"x" * 30).decode(), session_id=session)
        assert decoded == []  # never decoded the oversize payload

    def test_whitespace_new_name_is_no_change(self, session, mock_client):
        with pytest.raises(ValueError, match="new_name, color, or both"):
            wf.rename_tag("p", "x", new_name="   ", session_id=session)
        mock_client.api.get.assert_not_called()

    def test_rename_returns_the_tags_actual_color(self, session, mock_client):
        # No color given: Taiga keeps the old one; the result must not say null.
        route(
            mock_client,
            get={"/projects/by_slug": PROJECT, "/projects/1/tags_colors": {"y": "#123456"}},
            post={"/projects/1/edit_tag": None},
        )
        result = wf.rename_tag("p", "x", new_name="y", session_id=session)
        assert result == {"status": "tag_updated", "tag": "x", "new_name": "y", "color": "#123456"}

    def test_admin_message_names_both_causes(self, session, mock_client):
        from src.taiga_client import TaigaAuthenticationError

        mock_client.api.get.return_value = PROJECT
        route(mock_client, post={"/projects/1/mix_tags": TaigaAuthenticationError(403, "no", {})})
        with pytest.raises(TaigaAuthenticationError, match="or you are not a member"):
            wf.merge_tags("p", ["a"], "c", session_id=session)


# ---------------------------------------------------------------------------
# v3 B8: project admin tools. Shapes and rules from real Taiga / taiga-back.
# ---------------------------------------------------------------------------

STATUSES = [
    {"id": 105, "name": "New", "order": 0, "color": "#999", "is_closed": False},
    {"id": 112, "name": "Done", "order": 7, "color": "#0f0", "is_closed": True},
    {"id": 106, "name": "Review", "order": 1, "color": "#00f", "is_closed": False},
]


def _admin_route(mock_client, **extra):
    route(
        mock_client,
        get={
            "/projects/by_slug": PROJECT,
            "/userstory-statuses": STATUSES,
            **extra.pop("get", {}),
        },
        **extra,
    )


class TestProjectSettings:
    def test_list_sorted_by_order_unpaginated(self, session, mock_client):
        _admin_route(mock_client)
        result = wf.list_project_settings("p", "story_statuses", session_id=session)
        assert [r["name"] for r in result] == ["New", "Review", "Done"]
        assert result[2] == {
            "id": 112,
            "name": "Done",
            "order": 7,
            "color": "#0f0",
            "is_closed": True,
        }
        mock_client.api.get.assert_any_call(
            "/userstory-statuses", params={"project": 1}, headers={"x-disable-pagination": "True"}
        )

    def test_create_status_and_cache_cleared(self, session, mock_client):
        wf._session_cache[session] = {"statuses_story_1": ["stale"]}
        _admin_route(
            mock_client, post={"/userstory-statuses": {"id": 120, "name": "QA", "order": 9}}
        )
        result = wf.create_project_setting(
            "p", "story_statuses", "QA", color="#123456", is_closed=False, session_id=session
        )
        assert result["status"] == "created" and result["id"] == 120
        assert sent(mock_client, "post", "/userstory-statuses") == [
            {"project": 1, "name": "QA", "color": "#123456", "is_closed": False}
        ]
        assert session not in wf._session_cache  # set_story_status("QA") will see it

    def test_custom_attribute_field_type_maps_to_type(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, post={"/task-custom-attributes": {"id": 3, "name": "Env"}})
        wf.create_project_setting(
            "p",
            "task_custom_attributes",
            "Env",
            description="d",
            field_type="dropdown",
            session_id=session,
        )
        assert sent(mock_client, "post", "/task-custom-attributes") == [
            {"project": 1, "name": "Env", "description": "d", "type": "dropdown"}
        ]

    @pytest.mark.parametrize(
        "kind,kwargs",
        [
            ("priorities", {"is_closed": True}),
            ("points", {"color": "#fff"}),
            ("story_statuses", {"value": 3}),
            ("story_custom_attributes", {"color": "#fff"}),
        ],
    )
    def test_field_not_for_kind_rejected_before_any_call(self, session, mock_client, kind, kwargs):
        with pytest.raises(ValueError, match="do not apply"):
            wf.create_project_setting("p", kind, "X", session_id=session, **kwargs)
        mock_client.api.get.assert_not_called()

    def test_bad_kind_and_field_type(self, session, mock_client):
        with pytest.raises(ValueError, match="Invalid kind"):
            wf.list_project_settings("p", "swimlanes", session_id=session)
        with pytest.raises(ValueError, match="field_type must be one of"):
            wf.create_project_setting(
                "p", "epic_custom_attributes", "X", field_type="blob", session_id=session
            )

    def test_update_by_name_case_insensitive(self, session, mock_client):
        _admin_route(
            mock_client, patch={"/userstory-statuses/106": {"id": 106, "name": "Peer review"}}
        )
        wf.update_project_setting(
            "p",
            "story_statuses",
            "review",
            new_name="Peer review",
            color="#abc",
            session_id=session,
        )
        assert sent(mock_client, "patch", "/userstory-statuses/106") == [
            {"color": "#abc", "name": "Peer review"}
        ]

    def test_update_unknown_name_lists_existing(self, session, mock_client):
        _admin_route(mock_client)
        with pytest.raises(ValueError, match=r"No story_statuses entry named 'QA'.*New"):
            wf.update_project_setting("p", "story_statuses", "QA", color="#fff", session_id=session)

    def test_delete_status_requires_move_to_before_any_call(self, session, mock_client):
        with pytest.raises(ValueError, match="needs move_to"):
            wf.delete_project_setting("p", "story_statuses", "Review", session_id=session)
        mock_client.api.get.assert_not_called()

    def test_delete_status_with_move_to(self, session, mock_client):
        _admin_route(mock_client, delete={"/userstory-statuses/106": None})
        result = wf.delete_project_setting(
            "p", "story_statuses", "Review", move_to="New", session_id=session
        )
        assert result == {
            "status": "deleted",
            "kind": "story_statuses",
            "id": 106,
            "name": "Review",
            "moved_to": "New",
        }
        mock_client.api.delete.assert_called_once_with(
            "/userstory-statuses/106", params={"moveTo": 105}
        )

    def test_delete_move_to_itself_rejected(self, session, mock_client):
        _admin_route(mock_client)
        with pytest.raises(ValueError, match="different entry"):
            wf.delete_project_setting(
                "p", "story_statuses", "New", move_to="new", session_id=session
            )
        mock_client.api.delete.assert_not_called()

    def test_custom_attribute_delete_has_no_move_to(self, session, mock_client):
        with pytest.raises(ValueError, match="move_to does not apply"):
            wf.delete_project_setting(
                "p", "story_custom_attributes", "Env", move_to="x", session_id=session
            )

    def test_reorder_uses_kind_specific_bulk_param(self, session, mock_client):
        # taiga-back bulk_update_param per viewset (the pre-v3 tool sent `bulk_orders`).
        _admin_route(mock_client, post={"/userstory-statuses/bulk_update_order": None})
        result = wf.reorder_project_settings(
            "p", "story_statuses", ["Done", "New", "Review"], session_id=session
        )
        assert result["order"] == ["Done", "New", "Review"]
        assert sent(mock_client, "post", "/userstory-statuses/bulk_update_order") == [
            {"project": 1, "bulk_userstory_statuses": [[112, 0], [105, 1], [106, 2]]}
        ]

    def test_reorder_must_list_every_entry(self, session, mock_client):
        _admin_route(mock_client)
        with pytest.raises(ValueError, match=r"missing: \['Review'\]"):
            wf.reorder_project_settings("p", "story_statuses", ["Done", "New"], session_id=session)
        mock_client.api.post.assert_not_called()

    def test_403_names_project_admin(self, session, mock_client):
        from src.taiga_client import TaigaAuthenticationError

        _admin_route(
            mock_client,
            post={"/userstory-statuses": TaigaAuthenticationError(403, "no", {})},
        )
        with pytest.raises(
            TaigaAuthenticationError, match="create_project_setting requires project admin"
        ):
            wf.create_project_setting("p", "story_statuses", "QA", session_id=session)


class TestFindSetting:
    POINTS = [{"id": 3, "name": "1/2"}, {"id": 5, "name": "3"}]

    def test_numeric_name_wins_over_id(self):
        from src.server.tools.admin import _find_setting

        assert _find_setting(self.POINTS, "points", 3)["name"] == "3"

    def test_id_fallback_accepts_int_and_digit_string(self):
        from src.server.tools.admin import _find_setting

        assert _find_setting(self.POINTS, "points", 5)["name"] == "3"
        assert _find_setting(self.POINTS, "points", "5")["name"] == "3"


class TestCustomAttributeValues:
    def test_maps_names_to_ids_merges_and_clears(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/userstory-custom-attributes": [
                    {"id": 1, "name": "Env", "order": 0},
                    {"id": 2, "name": "Owner", "order": 1},
                    {"id": 3, "name": "Old", "order": 2},
                ],
                "/userstories/custom-attributes-values/50": {
                    "attributes_values": {"2": "kept", "3": "gone"},
                    "version": 4,
                },
            },
            patch={
                "/userstories/custom-attributes-values/50": {
                    "attributes_values": {"1": "prod", "2": "kept"},
                    "version": 5,
                }
            },
        )
        result = wf.set_custom_attribute_values(
            "p", 3, {"env": "prod", "Old": None}, session_id=session
        )
        assert sent(mock_client, "patch", "/userstories/custom-attributes-values/50") == [
            {"attributes_values": {"2": "kept", "3": None, "1": "prod"}, "version": 4}
        ]
        assert result["values"] == {"Env": "prod", "Owner": "kept"}

    def test_unknown_attribute_name(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            get={
                "/userstories/by_ref": STORY,
                "/userstory-custom-attributes": [{"id": 1, "name": "Env"}],
            },
        )
        with pytest.raises(ValueError, match="No story_custom_attributes entry named 'Nope'"):
            wf.set_custom_attribute_values("p", 3, {"Nope": 1}, session_id=session)
        mock_client.api.patch.assert_not_called()


MEMBERS = [
    {
        "user": 6,
        "full_name": "Jane",
        "user_email": "jdoe@x",
        "email": "",
        "role_name": "PO",
        "is_admin": True,
        "is_owner": False,
    },
    {
        "user": None,
        "full_name": None,
        "user_email": None,
        "email": "new@x",
        "role_name": "Dev",
        "is_admin": False,
        "is_owner": False,
    },
]
ROLES = [
    {"id": 57, "name": "Dev Team", "order": 10},
    {"id": 58, "name": "Product Owner", "order": 20},
]


class TestMembers:
    def _route(self, mock_client, **extra):
        mock_client.list_resources.return_value = MEMBERS
        route(mock_client, get={"/projects/by_slug": PROJECT, "/roles": ROLES}, **extra)

    def test_list_members_and_roles(self, session, mock_client):
        self._route(mock_client)
        result = wf.list_members("p", session_id=session)
        assert result["roles"] == ["Dev Team", "Product Owner"]
        assert result["members"][0]["email"] == "jdoe@x"  # user_email, not the blank email
        assert result["members"][1]["pending"] is True

    def test_invite_payload_uses_username_and_role_id(self, session, mock_client):
        # taiga-back _MemberBulkValidator: username + role_id (pre-v3 sent `email`).
        self._route(mock_client, post={"/memberships/bulk_create": []})
        result = wf.invite_members(
            "p", ["a@x", " b@x "], "dev team", message="Welcome", session_id=session
        )
        assert result == {"status": "invited", "emails": ["a@x", "b@x"], "role": "Dev Team"}
        assert sent(mock_client, "post", "/memberships/bulk_create") == [
            {
                "project_id": 1,
                "bulk_memberships": [
                    {"username": "a@x", "role_id": 57},
                    {"username": "b@x", "role_id": 57},
                ],
                "invitation_extra_text": "Welcome",
            }
        ]

    def test_existing_member_refused_before_any_invite(self, session, mock_client):
        # Real Taiga answers a 500 for this; refuse with a clear message instead.
        self._route(mock_client)
        with pytest.raises(ValueError, match=r"Already members of 'p': \['JDOE@x'\]"):
            wf.invite_members("p", ["JDOE@x"], "Dev Team", session_id=session)
        mock_client.api.post.assert_not_called()

    def test_unknown_role_lists_roles(self, session, mock_client):
        self._route(mock_client)
        with pytest.raises(ValueError, match="No role named 'Boss'.*Dev Team"):
            wf.invite_members("p", ["a@x"], "Boss", session_id=session)
        mock_client.api.post.assert_not_called()

    def test_overlong_message_refused_not_truncated(self, session, mock_client):
        with pytest.raises(ValueError, match="256 characters; Taiga allows at most 255"):
            wf.invite_members("p", ["a@x"], "Dev Team", message="m" * 256, session_id=session)
        mock_client.api.get.assert_not_called()

    @pytest.mark.parametrize(
        "emails,role,match",
        [([], "Dev", "at least one"), (["a@x", "A@x"], "Dev", "twice"), (["a@x"], " ", "role")],
    )
    def test_invite_input_validation(self, session, mock_client, emails, role, match):
        with pytest.raises(ValueError, match=match):
            wf.invite_members("p", emails, role, session_id=session)
        mock_client.api.get.assert_not_called()


# ---------------------------------------------------------------------------
# v3 B9: create_items, link_to_epic, reorder_stories. Payloads per taiga-back.
# ---------------------------------------------------------------------------


class TestCreateItems:
    @pytest.mark.parametrize(
        "entity_type,endpoint,key",
        [
            ("story", "/userstories/bulk_create", "bulk_stories"),
            ("issue", "/issues/bulk_create", "bulk_issues"),
            ("epic", "/epics/bulk_create", "bulk_epics"),
        ],
    )
    def test_one_newline_joined_payload(self, session, mock_client, entity_type, endpoint, key):
        mock_client.api.get.return_value = PROJECT
        route(
            mock_client,
            post={
                endpoint: [
                    {"ref": 7, "id": 70, "subject": "a"},
                    {"ref": 8, "id": 80, "subject": "b"},
                ]
            },
        )
        result = wf.create_items("p", [" a ", "b", ""], entity_type=entity_type, session_id=session)
        assert result["count"] == 2 and [i["ref"] for i in result["items"]] == [7, 8]
        assert sent(mock_client, "post", endpoint) == [{"project_id": 1, key: "a\nb"}]

    @pytest.mark.parametrize(
        "subjects,match",
        [([], "at least one"), (["a\nb"], "line breaks"), (["x"] * 101, "At most 100")],
    )
    def test_guards_before_any_call(self, session, mock_client, subjects, match):
        with pytest.raises(ValueError, match=match):
            wf.create_items("p", subjects, session_id=session)
        mock_client.api.get.assert_not_called()

    def test_task_not_supported(self, session, mock_client):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            wf.create_items("p", ["a"], entity_type="task", session_id=session)


class TestLinkToEpic:
    def _route(self, mock_client, related):
        mock_client.api.get.return_value = PROJECT
        mock_client.list_resources.return_value = [
            {"ref": 1, "id": 10},
            {"ref": 2, "id": 20},
            {"ref": 3, "id": 30},
        ]
        route(
            mock_client,
            get={"/epics/by_ref": {"id": 500, "ref": 9}, "/epics/500/related_userstories": related},
            post={"/epics/500/related_userstories": {}},
        )

    def test_links_new_skips_already_linked(self, session, mock_client):
        self._route(mock_client, [{"epic": 500, "user_story": 20, "order": 1}])
        result = wf.link_to_epic("p", 9, [1, 2, 3, 1], session_id=session)
        assert result == {
            "status": "linked",
            "epic_ref": 9,
            "linked": [1, 3],
            "already_linked": [2],
        }
        assert sent(mock_client, "post", "/epics/500/related_userstories") == [
            {"epic": 500, "user_story": 10},
            {"epic": 500, "user_story": 30},
        ]
        for c in mock_client.api.post.call_args_list:
            assert "bulk_create" not in c.args[0]  # #143

    def test_partial_failure_names_what_was_linked(self, session, mock_client):
        self._route(mock_client, [])
        route(
            mock_client,
            post={"/epics/500/related_userstories": Seq({}, TaigaException("boom"), {})},
        )
        with pytest.raises(TaigaException, match=r"Linked \[1, 3\] to epic #9; failed: #2: boom"):
            wf.link_to_epic("p", 9, [1, 2, 3], session_id=session)

    def test_linked_meanwhile_counts_as_already_linked(self, session, mock_client):
        # Another client links #2 between our read and our POST: Taiga answers 400.
        from src.taiga_client import TaigaBadRequestError

        self._route(mock_client, [])
        dup = TaigaBadRequestError(
            400, "__all__: Related user story with this User story and Epic already exists.", {}
        )
        route(mock_client, post={"/epics/500/related_userstories": Seq({}, dup, {})})
        result = wf.link_to_epic("p", 9, [1, 2, 3], session_id=session)
        assert result["linked"] == [1, 3] and result["already_linked"] == [2]

    def test_unknown_epic_real_404(self, session, mock_client):
        mock_client.api.get.return_value = PROJECT
        route(mock_client, get={"/epics/by_ref": _api_error(TaigaNotFoundError, 404)})
        with pytest.raises(ValueError, match="Epic #9 not found"):
            wf.link_to_epic("p", 9, [1], session_id=session)


def _stories(*rows):
    return [
        {"ref": r, "id": r * 10, "milestone": m, "status": st, "swimlane": None}
        for r, m, st in rows
    ]


class TestReorderStories:
    def _route(self, mock_client, stories, milestones=None):
        mock_client.api.get.return_value = PROJECT

        def lr(resource, project_id=None, **kw):
            return {"user_stories": stories, "milestones": milestones or []}[resource]

        mock_client.list_resources.side_effect = lr
        route(
            mock_client,
            post={
                "/userstories/bulk_update_backlog_order": [],
                "/userstories/bulk_update_kanban_order": [],
            },
        )

    def test_backlog_before_anchor(self, session, mock_client):
        self._route(mock_client, _stories((1, None, 5), (2, None, 5), (3, None, 5)))
        wf.reorder_stories("p", [3, 2], before_ref=1, session_id=session)
        assert sent(mock_client, "post", "/userstories/bulk_update_backlog_order") == [
            {"project_id": 1, "bulk_userstories": [30, 20], "before_userstory_id": 10}
        ]

    def test_sprint_view_sends_milestone_and_after(self, session, mock_client):
        self._route(
            mock_client, _stories((1, 9, 5), (2, 9, 5)), milestones=[{"id": 9, "name": "S1"}]
        )
        wf.reorder_stories("p", [1], view="sprint", sprint="S1", after_ref=2, session_id=session)
        assert sent(mock_client, "post", "/userstories/bulk_update_backlog_order") == [
            {"project_id": 1, "bulk_userstories": [10], "after_userstory_id": 20, "milestone_id": 9}
        ]

    def test_kanban_needs_one_status(self, session, mock_client):
        self._route(mock_client, _stories((1, None, 5), (2, None, 5)))
        wf.reorder_stories("p", [2, 1], view="kanban", session_id=session)
        assert sent(mock_client, "post", "/userstories/bulk_update_kanban_order") == [
            {"project_id": 1, "bulk_userstories": [20, 10], "status_id": 5}
        ]
        self._route(mock_client, _stories((1, None, 5), (2, None, 6)))
        with pytest.raises(ValueError, match="share one status"):
            wf.reorder_stories("p", [2, 1], view="kanban", session_id=session)

    def test_refuses_a_story_from_another_column(self, session, mock_client):
        # The endpoint would SET milestone=None on it (and its tasks): never move silently.
        self._route(mock_client, _stories((1, None, 5), (2, 9, 5)))
        with pytest.raises(ValueError, match=r"Stories \[2\] are not in the backlog"):
            wf.reorder_stories("p", [1, 2], session_id=session)
        mock_client.api.post.assert_not_called()

    def test_refuses_anchor_from_another_column(self, session, mock_client):
        self._route(mock_client, _stories((1, None, 5), (2, 9, 5)))
        with pytest.raises(ValueError, match=r"\[2\] are not in the backlog"):
            wf.reorder_stories("p", [1], before_ref=2, session_id=session)
        mock_client.api.post.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"story_refs": []}, "at least one"),
            ({"story_refs": [1], "view": "board"}, "view must be"),
            ({"story_refs": [1], "view": "sprint"}, "needs the sprint"),
            ({"story_refs": [1], "sprint": "S1"}, "only applies"),
            ({"story_refs": [1], "before_ref": 2, "after_ref": 3}, "not both"),
            ({"story_refs": [1, 2], "before_ref": 2}, "cannot be one of"),
        ],
    )
    def test_argument_guards_before_any_call(self, session, mock_client, kwargs, match):
        with pytest.raises(ValueError, match=match):
            wf.reorder_stories("p", session_id=session, **kwargs)
        mock_client.api.get.assert_not_called()


# ---------------------------------------------------------------------------
# Every by_ref lookup turns real Taiga's 404 into a clear
# "not found" (the old `if not item` guards never fired: by_ref never returns
# an empty body).
# ---------------------------------------------------------------------------


def _not_found(path):
    return {path: _api_error(TaigaNotFoundError, 404, "No match")}


@pytest.mark.parametrize(
    "call,path,match",
    [
        (
            lambda s: wf.get_story("p", 5, session_id=s),
            "/userstories/by_ref",
            "User story #5 not found",
        ),
        (lambda s: wf.get_task("p", 5, session_id=s), "/tasks/by_ref", "Task #5 not found"),
        (lambda s: wf.get_issue("p", 5, session_id=s), "/issues/by_ref", "Issue #5 not found"),
        (
            lambda s: wf.update_story("p", 5, subject="x", session_id=s),
            "/userstories/by_ref",
            "User story #5 not found",
        ),
        (
            lambda s: wf.update_task("p", 5, subject="x", session_id=s),
            "/tasks/by_ref",
            "Task #5 not found",
        ),
        (
            lambda s: wf.update_issue("p", 5, subject="x", session_id=s),
            "/issues/by_ref",
            "Issue #5 not found",
        ),
        (
            lambda s: wf.create_task("p", 5, "t", session_id=s),
            "/userstories/by_ref",
            "User story #5 not found",
        ),
        (
            lambda s: wf.break_down_story("p", 5, ["t"], session_id=s),
            "/userstories/by_ref",
            "User story #5 not found",
        ),
        (
            lambda s: wf.assign_item("p", 5, "bob", session_id=s),
            "/userstories/by_ref",
            "#5 not found",
        ),
        (
            lambda s: wf.get_epic_overview("p", 5, session_id=s),
            "/epics/by_ref",
            "Epic #5 not found",
        ),
        (
            lambda s: wf.create_story("p", "x", epic=5, session_id=s),
            "/epics/by_ref",
            "Epic #5 not found",
        ),
    ],
)
def test_real_404_on_by_ref_is_a_clear_not_found(session, mock_client, call, path, match):
    mock_client.api.get.return_value = PROJECT
    mock_client.list_resources.return_value = [{"user": 7, "full_name": "Bob", "email": "bob@x"}]
    route(mock_client, get=_not_found(path))
    with pytest.raises(ValueError, match=match):
        call(session)
    mock_client.api.post.assert_not_called()
    mock_client.api.patch.assert_not_called()


def test_set_status_tools_real_404(session, mock_client):
    mock_client.api.get.return_value = PROJECT
    mock_client.list_resources.return_value = [{"id": 1, "name": "Done"}]
    route(mock_client, get={**_not_found("/userstories/by_ref"), **_not_found("/tasks/by_ref")})
    with pytest.raises(ValueError, match="User story #5 not found"):
        wf.set_story_status("p", 5, "Done", session_id=session)
    with pytest.raises(ValueError, match="Task #5 not found"):
        wf.set_task_status("p", 5, "Done", session_id=session)
    mock_client.api.patch.assert_not_called()


def test_update_story_relink_to_unknown_epic_real_404(session, mock_client):
    route(
        mock_client,
        get={
            "/projects/by_slug": PROJECT,
            "/userstories/by_ref": {"id": 100, "ref": 5, "version": 1, "epics": []},
            **_not_found("/epics/by_ref"),
        },
    )
    with pytest.raises(ValueError, match="Epic #9 not found"):
        wf.update_story("p", 5, epic=9, session_id=session)
    mock_client.api.delete.assert_not_called()
    mock_client.api.post.assert_not_called()


def test_update_task_reparent_to_unknown_story_real_404(session, mock_client):
    route(
        mock_client,
        get={
            "/projects/by_slug": PROJECT,
            "/tasks/by_ref": {"id": 200, "ref": 7, "version": 3},
            **_not_found("/userstories/by_ref"),
        },
    )
    with pytest.raises(ValueError, match="User story #44 not found"):
        wf.update_task("p", 7, story_ref=44, session_id=session)
    mock_client.api.patch.assert_not_called()


def test_get_wiki_unknown_slug_real_404(session, mock_client):
    route(mock_client, get=_not_found("/wiki/by_slug"))
    mock_client.api.get.return_value = PROJECT
    with pytest.raises(ValueError, match="Wiki page 'nope' not found"):
        wf.get_wiki("p", slug="nope", session_id=session)


@pytest.mark.parametrize("project,path", [("ghost", "/projects/by_slug"), (999, "/projects/999")])
def test_unknown_project_real_404(session, mock_client, project, path):
    route(mock_client, get=_not_found(path))
    with pytest.raises(ValueError, match="not found"):
        wf.get_story(project, 5, session_id=session)


def test_readme_tool_table_matches_registered_tools():
    """README's Tools table lists exactly the registered tools (it once said ~34/107)."""
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    table = readme.split("## Tools", 1)[1].split("\n\n", 2)[1]
    documented = set(re.findall(r"`([a-z_]+)`", table))
    registered = {t.name for t in wf.mcp._tool_manager.list_tools()}
    assert documented == registered, {
        "missing_from_readme": sorted(registered - documented),
        "not_a_tool": sorted(documented - registered),
    }
