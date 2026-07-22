"""Unit tests for server_workflow.py — name resolution helpers and tool smoke tests."""

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest
from pytaigaclient.exceptions import TaigaAPIError, TaigaException

import src.server_workflow as wf

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
        mock_client.api.projects.get.return_value = {"id": 42, "name": "Test", "slug": "test"}
        result = wf._resolve_project(mock_client, 42)
        mock_client.api.projects.get.assert_called_once_with(42)
        assert result["id"] == 42

    def test_resolves_numeric_string_as_id(self, mock_client):
        mock_client.api.projects.get.return_value = {"id": 7, "name": "Test", "slug": "test"}
        wf._resolve_project(mock_client, "7")
        mock_client.api.projects.get.assert_called_once_with(7)

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
        return [
            {
                "user": 42,
                "full_name": "Alice Martin",
                "email": "alice@example.com",
                "user_extra_info": {
                    "username": "alice",
                    "full_name_display": "Alice Martin",
                },
            }
        ]

    def test_resolves_by_username(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, "alice", "sess") == 42

    def test_resolves_by_email(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, "alice@example.com", "sess") == 42

    def test_resolves_by_full_name(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        assert wf._resolve_user(mock_client, 1, "Alice Martin", "sess") == 42

    def test_raises_when_not_found(self, mock_client):
        mock_client.list_resources.return_value = self._members()
        with pytest.raises(ValueError, match="not found"):
            wf._resolve_user(mock_client, 1, "bob", "sess")

    def test_resolves_past_member_with_null_fields(self, mock_client):
        # Regression for pytaiga-mcp#120: a pending-invite membership with null
        # email/full_name/user_extra_info must not abort resolution before the
        # valid member is reached (previously raised 'NoneType' ... 'lower').
        members = [
            {"user": None, "email": None, "full_name": None, "user_extra_info": None},
            *self._members(),
        ]
        mock_client.list_resources.return_value = members
        assert wf._resolve_user(mock_client, 1, "alice", "sess") == 42

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
        mock_client.api.user_stories.create.return_value = {
            "id": 100,
            "ref": 5,
            "subject": "As a user",
            "milestone_extra_info": None,
        }
        result = wf.create_story("test", "As a user", session_id=session)
        assert result["status"] == "created"
        assert result["ref"] == 5

    def test_creates_story_with_assignee(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "test", "name": "Test"}
        mock_client.list_resources.return_value = [
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@example.com",
                "user_extra_info": {"username": "bob", "full_name_display": "Bob"},
            }
        ]
        mock_client.api.user_stories.create.return_value = {
            "id": 200,
            "ref": 10,
            "subject": "Story",
            "milestone_extra_info": None,
        }
        result = wf.create_story("test", "Story", assignee="bob", session_id=session)
        assert result["status"] == "created"
        call_kwargs = mock_client.api.user_stories.create.call_args.kwargs
        assert call_kwargs["assigned_to"] == 7

    def test_creates_story_with_epic_link(self, session, mock_client):
        # api.get: 1st resolves the project (by_slug), 2nd resolves the epic (by_ref).
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "test", "name": "Test"},
            {"id": 99, "ref": 7, "subject": "Epic"},
        ]
        mock_client.api.user_stories.create.return_value = {
            "id": 100,
            "ref": 5,
            "subject": "Story",
            "milestone_extra_info": None,
        }
        result = wf.create_story("test", "Story", epic=7, session_id=session)
        assert result["status"] == "created"
        assert result["epic_linked"] is True
        # Taiga's related_userstories endpoint requires 'epic' + 'user_story'
        # (not project_id/user_story_id) — same payload as update_story relink.
        mock_client.api.post.assert_called_once_with(
            "/epics/99/related_userstories",
            json={"epic": 99, "user_story": 100},
        )

    def test_epic_not_found_creates_no_story(self, session, mock_client):
        # Epic is validated before creation, so a bad ref must NOT leave an
        # orphaned (created-but-unlinked) story behind.
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "test", "name": "Test"},
            None,  # /epics/by_ref → not found
        ]
        with pytest.raises(ValueError, match="Epic #7 not found"):
            wf.create_story("test", "Story", epic=7, session_id=session)
        mock_client.api.user_stories.create.assert_not_called()
        mock_client.api.post.assert_not_called()


class TestSetStoryStatus:
    def test_resolves_status_and_updates(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [{"id": 99, "name": "Done"}]
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 3,
            "version": 1,
        }
        mock_client.api.user_stories.edit.return_value = {
            "ref": 3,
            "status_extra_info": {"name": "Done"},
            "is_closed": True,
        }
        result = wf.set_story_status("p", 3, "Done", session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True


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
                    "milestone_extra_info": {"name": "Sprint 1"},
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


class TestUpsertWiki:
    def test_creates_new_page_when_not_found(self, session, mock_client):
        # project resolution
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},  # _resolve_project
            None,  # /wiki/by_slug → not found
        ]
        mock_client.api.wiki.create.return_value = {"id": 9, "slug": "my-page"}
        result = wf.upsert_wiki("p", "my-page", "Content here", session_id=session)
        assert result["status"] == "created"

    def test_updates_existing_page(self, session, mock_client):
        mock_client.api.get.side_effect = [
            {"id": 1, "slug": "p", "name": "P"},
            {"id": 9, "slug": "my-page", "version": 3},
        ]
        mock_client.api.wiki.edit.return_value = {"id": 9, "slug": "my-page"}
        result = wf.upsert_wiki("p", "my-page", "Updated content", session_id=session)
        assert result["status"] == "updated"
        kwargs = mock_client.api.wiki.edit.call_args.kwargs
        assert kwargs["version"] == 3
        assert kwargs["data"] == {"slug": "my-page", "content": "Updated content"}


class TestUpdateIssue:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _issue(self):
        return {"id": 300, "ref": 5, "subject": "Bug", "version": 2}

    def test_resolves_status_by_name(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        mock_client.list_resources.return_value = [{"id": 20, "name": "In Progress"}]
        mock_client.api.issues.edit.return_value = {
            "ref": 5,
            "subject": "Bug",
            "status_extra_info": {"name": "In Progress"},
            "priority_extra_info": None,
            "severity_extra_info": None,
            "type_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_issue("p", 5, status="In Progress", session_id=session)
        kwargs = mock_client.api.issues.edit.call_args.kwargs
        assert kwargs["version"] == 2
        assert kwargs["data"]["status"] == 20

    def test_resolves_priority_by_name(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        mock_client.list_resources.return_value = [{"id": 3, "name": "High"}]
        mock_client.api.issues.edit.return_value = {
            "ref": 5,
            "subject": "Bug",
            "status_extra_info": None,
            "priority_extra_info": {"name": "High"},
            "severity_extra_info": None,
            "type_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_issue("p", 5, priority="High", session_id=session)
        kwargs = mock_client.api.issues.edit.call_args.kwargs
        assert kwargs["version"] == 2
        assert kwargs["data"]["priority"] == 3

    def test_no_op_raises(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_issue("p", 5, session_id=session)

    def test_data_does_not_contain_version(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        mock_client.list_resources.return_value = [{"id": 20, "name": "Done"}]
        mock_client.api.issues.edit.return_value = {
            "ref": 5,
            "subject": "Bug",
            "status_extra_info": {"name": "Done"},
            "priority_extra_info": None,
            "severity_extra_info": None,
            "type_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_issue("p", 5, status="Done", session_id=session)
        kwargs = mock_client.api.issues.edit.call_args.kwargs
        assert "version" not in kwargs["data"]


# ---------------------------------------------------------------------------
# Bug fixes from PR #72 review
# ---------------------------------------------------------------------------


class TestSessionStatusParity:
    def test_returns_session_id_when_token_invalid(self, session, mock_client):
        from pytaigaclient.exceptions import TaigaException

        mock_client.api.users.get_me.side_effect = TaigaException("token expired")
        result = wf.session_status(session_id=session)
        assert result["status"] == "inactive"
        assert result["reason"] == "token_invalid"
        assert result["session_id"] == session

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
        mock_client.api.issues.create.return_value = {
            "id": 1,
            "ref": 9,
            "subject": "Bug",
        }

        wf.create_issue("p", "Bug", session_id=session)
        call_kwargs = mock_client.api.issues.create.call_args.kwargs
        # Extra fields are passed via the data dict, not as direct kwargs.
        assert call_kwargs["project"] == 1
        assert call_kwargs["subject"] == "Bug"
        data = call_kwargs["data"]
        # No None values should be sent to the API.
        assert "priority" not in data
        assert "severity" not in data
        assert "type" not in data
        # Default status is still wired up.
        assert data["status"] == 7


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
                "milestone_extra_info": None,
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
                "milestone_extra_info": None,
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
        mock_client.api.user_stories.get.assert_not_called()


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

    def test_epic_zero_unlinks_all_no_post(self, session, mock_client):
        # /projects/by_slug → project
        mock_client.api.get.return_value = self._project()
        # get_by_ref → story currently linked to two epics
        mock_client.api.user_stories.get_by_ref.return_value = self._story_with_epics(
            [{"id": 50, "ref": 1}, {"id": 51, "ref": 2}]
        )

        wf.update_story("p", 5, epic=0, session_id=session)

        # Two DELETEs (extracted from dict[id]), no POST, no new-epic lookup.
        assert mock_client.api.delete.call_count == 2
        mock_client.api.delete.assert_any_call("/epics/50/related_userstories/100")
        mock_client.api.delete.assert_any_call("/epics/51/related_userstories/100")
        mock_client.api.post.assert_not_called()

    def test_epic_relink_lookup_before_delete(self, session, mock_client):
        # First api.get call: /projects/by_slug → project
        # Second api.get call: /epics/by_ref → new epic
        mock_client.api.get.side_effect = [
            self._project(),
            {"id": 99, "ref": 7, "subject": "New Epic"},
        ]
        mock_client.api.user_stories.get_by_ref.return_value = self._story_with_epics(
            [{"id": 50, "ref": 1}]
        )

        wf.update_story("p", 5, epic=7, session_id=session)

        # Old link removed (one DELETE), new link created (one POST).
        mock_client.api.delete.assert_called_once_with("/epics/50/related_userstories/100")
        mock_client.api.post.assert_called_once_with(
            "/epics/99/related_userstories",
            json={"epic": 99, "user_story": 100},
        )

    def test_epic_not_found_does_not_unlink(self, session, mock_client):
        mock_client.api.get.side_effect = [
            self._project(),
            None,  # /epics/by_ref → not found
        ]
        mock_client.api.user_stories.get_by_ref.return_value = self._story_with_epics(
            [{"id": 50, "ref": 1}]
        )

        with pytest.raises(ValueError, match="Epic #7 not found"):
            wf.update_story("p", 5, epic=7, session_id=session)

        # Critical: validation runs BEFORE any mutation. The story's existing
        # link to epic #50 must be intact when the call fails.
        mock_client.api.delete.assert_not_called()
        mock_client.api.post.assert_not_called()

    def test_epic_link_when_story_has_none(self, session, mock_client):
        mock_client.api.get.side_effect = [
            self._project(),
            {"id": 99, "ref": 7, "subject": "New Epic"},
        ]
        mock_client.api.user_stories.get_by_ref.return_value = self._story_with_epics([])

        wf.update_story("p", 5, epic=7, session_id=session)

        mock_client.api.delete.assert_not_called()
        mock_client.api.post.assert_called_once_with(
            "/epics/99/related_userstories",
            json={"epic": 99, "user_story": 100},
        )

    def test_no_op_when_no_fields_and_no_epic(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = self._story_with_epics([])

        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_story("p", 5, session_id=session)


# ---------------------------------------------------------------------------
# Task tools (v2.1 — task/intent surface)
# ---------------------------------------------------------------------------


class TestCreateTask:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_creates_task_minimal(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 50, "ref": 42}
        mock_client.api.tasks.create.return_value = {
            "id": 200,
            "ref": 7,
            "subject": "Implement API",
        }
        result = wf.create_task("p", 42, "Implement API", session_id=session)
        assert result["status"] == "created"
        assert result["ref"] == 7
        assert result["parent_story_ref"] == 42
        call = mock_client.api.tasks.create.call_args
        assert call.kwargs["project"] == 1
        assert call.kwargs["subject"] == "Implement API"
        assert call.kwargs["data"]["user_story"] == 50

    def test_creates_task_with_assignee_and_status(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 50, "ref": 42}
        # Two list_resources calls — one for task_statuses, one for memberships.
        mock_client.list_resources.side_effect = [
            [{"id": 11, "name": "In Progress"}],
            [
                {
                    "user": 7,
                    "full_name": "Alice",
                    "email": "alice@example.com",
                    "user_extra_info": {"username": "alice", "full_name_display": "Alice"},
                }
            ],
        ]
        mock_client.api.tasks.create.return_value = {"id": 200, "ref": 7, "subject": "API"}
        wf.create_task("p", 42, "API", status="In Progress", assignee="alice", session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        assert data["status"] == 11
        assert data["assigned_to"] == 7

    def test_raises_when_parent_story_missing(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = None
        with pytest.raises(ValueError, match=r"User story #42 not found"):
            wf.create_task("p", 42, "T", session_id=session)


class TestUpdateTask:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _task(self):
        return {"id": 200, "ref": 7, "subject": "T", "version": 3, "user_story": 50}

    def _api_get(self, path, params=None):
        """Side-effect for api.get: route /tasks/by_ref to task fixture, everything else to project."""
        if "/tasks/by_ref" in str(path):
            return self._task()
        return self._project()

    def test_resolves_status_by_name(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        mock_client.list_resources.return_value = [{"id": 12, "name": "Done"}]
        mock_client.api.tasks.edit.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": {"name": "Done"},
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, status="Done", session_id=session)
        kwargs = mock_client.api.tasks.edit.call_args.kwargs
        assert kwargs["version"] == 3
        assert kwargs["data"]["status"] == 12

    def test_reparent_resolves_story_ref(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        # user_stories.get_by_ref is unaffected by the bug — mock it directly.
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 99, "ref": 44}
        mock_client.api.tasks.edit.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, story_ref=44, session_id=session)
        kwargs = mock_client.api.tasks.edit.call_args.kwargs
        assert kwargs["data"]["user_story"] == 99

    def test_sprint_move_supported(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        mock_client.list_resources.return_value = [{"id": 33, "name": "Sprint 2", "closed": False}]
        mock_client.api.tasks.edit.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, sprint="Sprint 2", session_id=session)
        kwargs = mock_client.api.tasks.edit.call_args.kwargs
        assert kwargs["data"]["milestone"] == 33

    def test_no_op_raises(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_task("p", 7, session_id=session)


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
        mock_client.api.tasks.edit.return_value = {
            "ref": 7,
            "status_extra_info": {"name": "Done"},
            "is_closed": True,
        }
        result = wf.set_task_status("p", 7, "Done", session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True
        # Confirm uses task_statuses, not userstory_statuses.
        mock_client.list_resources.assert_called_once_with("task_statuses", project_id=1)


class TestAddComment:
    """Regression guard for #87 — the task comment path must resolve ref→ID via
    the /tasks/by_ref endpoint with `params=`, NOT via tasks.get_by_ref() (which
    forwards a `query_params=` kwarg TaigaClient._request() rejects)."""

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
        mock_client.api.tasks.get_by_ref.assert_not_called()
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
        mock_client.api.user_stories.get_by_ref.assert_not_called()
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
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 33,
        }
        mock_client.api.post.return_value = [
            {"ref": 10, "subject": "design"},
            {"ref": 11, "subject": "API"},
            {"ref": 12, "subject": "tests"},
        ]
        result = wf.break_down_story("p", 42, ["design", "API", "tests"], session_id=session)
        assert result["status"] == "decomposed"
        assert result["tasks_created"] == 3
        # Single bulk POST, zero individual tasks.create calls.
        mock_client.api.post.assert_called_once()
        args, kwargs = mock_client.api.post.call_args.args, mock_client.api.post.call_args.kwargs
        assert args[0] == "/tasks/bulk_create"
        assert kwargs["json"]["us_id"] == 50
        assert kwargs["json"]["milestone_id"] == 33
        assert kwargs["json"]["bulk_tasks"] == "design\nAPI\ntests"
        mock_client.api.tasks.create.assert_not_called()

    def test_loop_path_when_story_in_backlog(self, session, mock_client):
        """No milestone on story → fall back to individual creates."""
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": None,
        }
        mock_client.api.tasks.create.side_effect = [
            {"ref": 10, "subject": "design"},
            {"ref": 11, "subject": "API"},
        ]
        result = wf.break_down_story("p", 42, ["design", "API"], session_id=session)
        assert result["tasks_created"] == 2
        # No bulk endpoint, one create per task.
        mock_client.api.post.assert_not_called()
        assert mock_client.api.tasks.create.call_count == 2

    def test_loop_path_when_per_task_overrides(self, session, mock_client):
        """Per-task overrides force the loop path even with a milestone."""
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 33,
        }
        mock_client.list_resources.return_value = [
            {
                "user": 7,
                "full_name": "Alice",
                "email": "a@x",
                "user_extra_info": {"username": "alice", "full_name_display": "Alice"},
            }
        ]
        mock_client.api.tasks.create.side_effect = [
            {"ref": 10, "subject": "design"},
            {"ref": 11, "subject": "API"},
        ]
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
        # Per-task override forced the loop, not the bulk endpoint.
        mock_client.api.post.assert_not_called()
        # Second call assigned to Alice.
        assert mock_client.api.tasks.create.call_args_list[1].kwargs["data"]["assigned_to"] == 7

    def test_rejects_malformed_entries(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": None,
        }
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
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 99,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        wf.create_task("p", 42, "X", session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        # Critical: task lands in the parent story's sprint by default.
        assert data["milestone"] == 99

    def test_no_milestone_when_parent_in_backlog(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": None,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        wf.create_task("p", 42, "X", session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        assert "milestone" not in data

    def test_sprint_zero_opts_out_even_if_parent_in_sprint(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 99,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        wf.create_task("p", 42, "X", sprint=0, session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        assert "milestone" not in data

    def test_explicit_sprint_overrides_parent(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 99,
        }
        mock_client.list_resources.return_value = [{"id": 77, "name": "Sprint 6", "closed": False}]
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        wf.create_task("p", 42, "X", sprint="Sprint 6", session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        assert data["milestone"] == 77


class TestBreakDownStoryOverrides:
    """Regression: loop path must honor all documented override keys."""

    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_loop_supports_tags_due_date_blocked(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": None,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
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
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        assert data["tags"] == ["a", "b"]
        assert data["due_date"] == "2026-06-30"
        assert data["is_blocked"] is True

    def test_rejects_unknown_override_key(self, session, mock_client):
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": None,
        }
        with pytest.raises(ValueError, match="Unknown per-task override keys"):
            wf.break_down_story("p", 42, [{"subject": "X", "color": "red"}], session_id=session)
        mock_client.api.tasks.create.assert_not_called()

    def test_loop_path_inherits_parent_milestone(self, session, mock_client):
        """Tasks created via loop path (overrides present) should still inherit milestone."""
        mock_client.api.get.return_value = self._project()
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 99,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        # Override forces loop path; milestone should still come through.
        wf.break_down_story(
            "p", 42, [{"subject": "X", "due_date": "2026-06-30"}], session_id=session
        )
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
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
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 99,
        }
        mock_client.api.tasks.create.return_value = {"id": 1, "ref": 1, "subject": "X"}
        wf.create_task("p", 42, "X", sprint="0", session_id=session)
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
        # str "0" must opt out, NOT fall through to _resolve_sprint(..., "0")
        # which would look up a non-existent milestone.
        assert "milestone" not in data
        mock_client.list_resources.assert_not_called()


class TestBreakDownStoryBulkShapeWarn:
    """Bulk endpoint returning a non-list should warn and coerce to []."""

    def test_warns_and_coerces_on_dict_response(self, session, mock_client, caplog):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 50,
            "ref": 42,
            "milestone": 33,
        }
        # Simulate an unexpected response shape from /tasks/bulk_create.
        mock_client.api.post.return_value = {"unexpected": "shape"}

        with caplog.at_level("WARNING", logger="src.server_workflow"):
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
            "username": "rva",
            "full_name_display": "Romain V",
            "full_name": "Romain Valtier",
            "email": "rva@tetra-ai.com",
            "bio": "Platform lead",
            "photo": "https://cdn.example.com/rva.jpg",
        }

    def test_returns_identity_fields(self, session, mock_client):
        mock_client.api.users.get_me.return_value = self._me()
        result = wf.get_current_user(session_id=session)
        assert result["id"] == 42
        assert result["username"] == "rva"
        assert result["full_name"] == "Romain V"
        assert result["email"] == "rva@tetra-ai.com"

    def test_prefers_full_name_display(self, session, mock_client):
        me = self._me()
        me["full_name_display"] = "Display Name"
        me["full_name"] = "Other Name"
        mock_client.api.users.get_me.return_value = me
        result = wf.get_current_user(session_id=session)
        assert result["full_name"] == "Display Name"

    def test_falls_back_to_full_name(self, session, mock_client):
        me = self._me()
        me["full_name_display"] = None
        mock_client.api.users.get_me.return_value = me
        result = wf.get_current_user(session_id=session)
        assert result["full_name"] == "Romain Valtier"

    def test_optional_fields_none_when_missing(self, session, mock_client):
        mock_client.api.users.get_me.return_value = {
            "id": 1,
            "username": "x",
            "email": "x@example.com",
        }
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


class TestGetStory:
    def test_returns_resolved_summary(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.user_stories.get_by_ref.return_value = {
            "ref": 3,
            "subject": "Story A",
            "status_extra_info": {"name": "In Progress"},
            "assigned_to_extra_info": {"full_name_display": "Alice"},
            "is_closed": False,
        }
        result = wf.get_story("p", 3, session_id=session)
        assert result["status"] == "In Progress"
        assert result["is_closed"] is False
        assert result["assignee"] == "Alice"
        mock_client.api.user_stories.get_by_ref.assert_called_once_with(ref=3, project=1)

    def test_raises_when_not_found(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.user_stories.get_by_ref.return_value = None
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
        # Must NOT route through the buggy tasks.get_by_ref helper.
        mock_client.api.tasks.get_by_ref.assert_not_called()

    def test_raises_when_not_found(self, session, mock_client):
        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return None
            return {"id": 1, "slug": "p", "name": "P"}

        mock_client.api.get.side_effect = api_get
        with pytest.raises(ValueError, match="not found"):
            wf.get_task("p", 99, session_id=session)


class TestGetIssue:
    def test_returns_resolved_summary(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.issues.get_by_ref.return_value = {
            "ref": 1212,
            "subject": "Bug",
            "status_extra_info": {"name": "New"},
            "priority_extra_info": {"name": "High"},
            "severity_extra_info": {"name": "Normal"},
            "type_extra_info": {"name": "Bug"},
            "assigned_to_extra_info": None,
            "is_closed": False,
        }
        result = wf.get_issue("p", 1212, session_id=session)
        assert result["status"] == "New"
        assert result["is_closed"] is False
        assert result["priority"] == "High"
        assert result["type"] == "Bug"
        mock_client.api.issues.get_by_ref.assert_called_once_with(ref=1212, project=1)

    def test_raises_when_not_found(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.issues.get_by_ref.return_value = None
        with pytest.raises(ValueError, match="not found"):
            wf.get_issue("p", 99, session_id=session)

    def test_is_closed_falls_back_to_status_extra_info(self, session, mock_client):
        # Payload with no top-level is_closed — closed-ness must be read off the
        # status row so the triage signal ("is it closed?") isn't silently False.
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.issues.get_by_ref.return_value = {
            "ref": 60,
            "subject": "Done bug",
            "status_extra_info": {"name": "Closed", "is_closed": True},
        }
        result = wf.get_issue("p", 60, session_id=session)
        assert result["status"] == "Closed"
        assert result["is_closed"] is True


# ---------------------------------------------------------------------------
# Sprint write tools — payload-shape regression cover (P0)
# ---------------------------------------------------------------------------


class TestPlanSprint:
    def test_creates_sprint_and_moves_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.milestones.create.return_value = {
            "id": 9,
            "name": "S1",
            "estimated_start": "2026-01-01",
            "estimated_finish": "2026-01-14",
        }
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
        mock_client.api.milestones.create.assert_called_once_with(
            project=1, name="S1", estimated_start="2026-01-01", estimated_finish="2026-01-14"
        )
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_milestone",
            json={
                "project_id": 1,
                "milestone_id": 9,
                "bulk_stories": [{"us_id": 120, "order": 0}, {"us_id": 150, "order": 1}],
            },
        )

    def test_creates_sprint_without_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.milestones.create.return_value = {
            "id": 9,
            "name": "S1",
            "estimated_start": "2026-01-01",
            "estimated_finish": "2026-01-14",
        }
        result = wf.plan_sprint("p", "S1", "2026-01-01", "2026-01-14", session_id=session)
        assert result["stories_assigned"] == 0
        mock_client.api.post.assert_not_called()


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
        mock_client.api.milestones.edit.return_value = {"id": 9, "name": "S1"}
        result = wf.close_sprint("p", "S1", session_id=session)
        assert result == {"status": "closed", "sprint": "S1", "id": 9}
        mock_client.api.milestones.edit.assert_called_once_with(9, closed=True, version=3)


class TestCreateEpic:
    def test_creates_epic_and_links_stories(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.epics.create.return_value = {"id": 7, "ref": 70, "subject": "Big"}
        mock_client.list_resources.return_value = [{"ref": 12, "id": 120}]  # user_stories
        result = wf.create_epic("p", "Big", story_refs=[12], session_id=session)
        assert result["status"] == "created"
        assert result["id"] == 7
        assert result["stories_linked"] == 1
        mock_client.api.epics.create.assert_called_once_with(project=1, subject="Big")
        mock_client.api.post.assert_called_once_with(
            "/epics/7/related_userstories/bulk_create",
            json={"project_id": 1, "bulk_userstories": [120]},
        )

    def test_creates_epic_minimal(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.epics.create.return_value = {"id": 7, "ref": 70, "subject": "Big"}
        result = wf.create_epic("p", "Big", session_id=session)
        assert result["stories_linked"] == 0
        mock_client.api.post.assert_not_called()

    def test_create_epic_with_color_and_assignee(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [  # memberships for _resolve_user
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@x",
                "user_extra_info": {"username": "bob"},
            }
        ]
        mock_client.api.epics.create.return_value = {"id": 7, "ref": 70, "subject": "Big"}
        wf.create_epic("p", "Big", color="#fff", assignee="bob", session_id=session)
        call = mock_client.api.epics.create.call_args.kwargs
        assert call["color"] == "#fff"
        assert call["assigned_to"] == 7


class TestAssignItem:
    def test_assigns_story(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [  # memberships
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@x",
                "user_extra_info": {"username": "bob"},
            }
        ]
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 100, "version": 2}
        mock_client.api.user_stories.edit.return_value = {
            "assigned_to_extra_info": {"full_name_display": "Bob"}
        }
        result = wf.assign_item("p", 5, "bob", session_id=session)
        assert result == {"ref": 5, "entity_type": "story", "assigned_to": "Bob"}
        mock_client.api.user_stories.edit.assert_called_once_with(100, assigned_to=7, version=2)

    def test_assigns_issue_via_entity_type(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.list_resources.return_value = [
            {
                "user": 7,
                "full_name": "Bob",
                "email": "bob@x",
                "user_extra_info": {"username": "bob"},
            }
        ]
        mock_client.api.issues.get_by_ref.return_value = {"id": 300, "version": 1}
        mock_client.api.issues.edit.return_value = {
            "assigned_to_extra_info": {"full_name_display": "Bob"}
        }
        result = wf.assign_item("p", 9, "bob", entity_type="issue", session_id=session)
        assert result["entity_type"] == "issue"
        mock_client.api.issues.edit.assert_called_once_with(300, assigned_to=7, version=1)

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
                "memberships": [
                    {
                        "user_extra_info": {"username": "bob"},
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
        assert result["team"][0]["username"] == "bob"

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


class TestLogin:
    def test_login_success_creates_session(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.return_value = True
        monkeypatch.setattr(wf, "TaigaClientWrapper", lambda host: wrapper)
        result = wf.login(host="https://taiga.example", username="u", password="p")
        assert "session_id" in result
        assert wf.active_sessions[result["session_id"]] is wrapper
        wrapper.login.assert_called_once_with(username="u", password="p")

    def test_login_failure_raises(self, monkeypatch):
        wrapper = MagicMock()
        wrapper.login.return_value = False
        monkeypatch.setattr(wf, "TaigaClientWrapper", lambda host: wrapper)
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


class _StubAPIError(TaigaAPIError):
    """Stand-in for a TaigaAPIError. Subclasses the real type so the wrapper's
    `except TaigaAPIError` catches it; bypasses the parent __init__ (unknown
    signature) and sets only the attributes the repair logic reads."""

    def __init__(self, status_code, error_detail, response):
        Exception.__init__(self, f"API Error {status_code}: {error_detail}")
        self.status_code = status_code
        self.error_detail = error_detail
        self.response = response


def _resp(json_value=None, raises=None):
    r = MagicMock()
    if raises is not None:
        r.json.side_effect = raises
    else:
        r.json.return_value = json_value
    return r


class TestRepairTaigaApiError:
    def test_rewrites_drf_dict_body(self):
        e = _StubAPIError(
            400,
            wf._TAIGA_API_ERROR_PLACEHOLDER,
            _resp({"user_story": ["This field is required."], "epic": ["This field is required."]}),
        )
        wf._repair_taiga_api_error(e)
        assert "user_story: This field is required." in e.error_detail
        assert "epic: This field is required." in e.error_detail
        assert e.args[0] == f"API Error 400: {e.error_detail}"

    def test_formats_scalar_and_nested_values(self):
        e = _StubAPIError(
            422,
            wf._TAIGA_API_ERROR_PLACEHOLDER,
            _resp({"detail": "bad", "meta": {"k": "v"}}),
        )
        wf._repair_taiga_api_error(e)
        assert "detail: bad" in e.error_detail
        assert 'meta: {"k": "v"}' in e.error_detail

    def test_noop_when_detail_not_placeholder(self):
        e = _StubAPIError(400, "real message", _resp({"x": ["y"]}))
        wf._repair_taiga_api_error(e)
        assert e.error_detail == "real message"

    def test_noop_when_no_response(self):
        e = _StubAPIError(400, wf._TAIGA_API_ERROR_PLACEHOLDER, None)
        wf._repair_taiga_api_error(e)
        assert e.error_detail == wf._TAIGA_API_ERROR_PLACEHOLDER

    def test_noop_when_body_not_parseable(self):
        e = _StubAPIError(400, wf._TAIGA_API_ERROR_PLACEHOLDER, _resp(raises=ValueError("no json")))
        wf._repair_taiga_api_error(e)
        assert e.error_detail == wf._TAIGA_API_ERROR_PLACEHOLDER

    def test_noop_when_body_empty_or_not_dict(self):
        e = _StubAPIError(400, wf._TAIGA_API_ERROR_PLACEHOLDER, _resp({}))
        wf._repair_taiga_api_error(e)
        assert e.error_detail == wf._TAIGA_API_ERROR_PLACEHOLDER
        e2 = _StubAPIError(400, wf._TAIGA_API_ERROR_PLACEHOLDER, _resp(["a", "b"]))
        wf._repair_taiga_api_error(e2)
        assert e2.error_detail == wf._TAIGA_API_ERROR_PLACEHOLDER


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
    """The wrapper around every tool body — error classification + repair."""

    def test_repairs_and_reraises_api_error(self):
        err = _StubAPIError(400, wf._TAIGA_API_ERROR_PLACEHOLDER, _resp({"field": ["required"]}))

        def op():
            raise err

        with pytest.raises(TaigaAPIError) as ei:
            wf._execute_taiga_operation("op", op)
        assert "field: required" in ei.value.error_detail

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
                        "user_extra_info": {"username": "bob"},
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
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 50, "milestone": None}

        def lr(resource, **kw):
            return {
                "milestones": [{"id": 9, "name": "S1"}],
                "task_statuses": [{"id": 3, "name": "In progress"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                        "user_extra_info": {"username": "bob"},
                    }
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        mock_client.api.tasks.create.return_value = {"id": 500, "ref": 80, "subject": "T"}
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
        data = mock_client.api.tasks.create.call_args.kwargs["data"]
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
        mock_client.api.get.side_effect = self._api_get()

        def lr(resource, **kw):
            return {
                "task_statuses": [{"id": 3, "name": "Done"}],
                "memberships": [
                    {
                        "user": 7,
                        "full_name": "Bob",
                        "email": "b@x",
                        "user_extra_info": {"username": "bob"},
                    }
                ],
                "milestones": [{"id": 9, "name": "S1"}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 60}
        mock_client.api.tasks.edit.return_value = {
            "ref": 80,
            "subject": "T2",
            "status_extra_info": {"name": "Done"},
        }
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
        call = mock_client.api.tasks.edit.call_args
        assert call.kwargs["version"] == 4
        data = call.kwargs["data"]
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
                        "user_extra_info": {"username": "bob"},
                    }
                ],
                "issue_statuses": [{"id": 40, "name": "New"}],
            }[resource]

        mock_client.list_resources.side_effect = lr
        mock_client.api.issues.create.return_value = {"id": 600, "ref": 90, "subject": "Bug"}
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
        data = mock_client.api.issues.create.call_args.kwargs["data"]
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
        mock_client.api.issues.get_by_ref.return_value = {"id": 600, "version": 2}

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
                        "user_extra_info": {"username": "bob"},
                    }
                ],
            }[resource]

        mock_client.list_resources.side_effect = lr
        mock_client.api.issues.edit.return_value = {"ref": 90, "subject": "B2"}
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
        call = mock_client.api.issues.edit.call_args
        assert call.kwargs["version"] == 2
        data = call.kwargs["data"]
        assert data["status"] == 40
        assert data["assigned_to"] == 7
        assert data["priority"] == 11
        assert data["severity"] == 21
        assert data["type"] == 31
        assert data["is_blocked"] is True
        assert data["tags"] == ["x"]

    def test_issue_not_found_raises(self, session, mock_client):
        mock_client.api.get.return_value = {"id": 1, "slug": "p", "name": "P"}
        mock_client.api.issues.get_by_ref.return_value = None
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
        mock_client.api.users.get_me.return_value = {"username": "bob"}
        result = wf.session_status(session_id=session)
        assert result == {"status": "active", "session_id": session, "username": "bob"}

    def test_session_status_not_found(self):
        result = wf.session_status(session_id="ghost")
        assert result == {"status": "inactive", "reason": "not_found", "session_id": "ghost"}
