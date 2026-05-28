"""Unit tests for server_workflow.py — name resolution helpers and tool smoke tests."""

import uuid
from unittest.mock import MagicMock

import pytest

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
        kwargs = mock_client.api.issues.create.call_args.kwargs
        # No None values should be sent to the API.
        assert "priority" not in kwargs
        assert "severity" not in kwargs
        assert "type" not in kwargs
        # Default status is still wired up.
        assert kwargs["status"] == 7


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
