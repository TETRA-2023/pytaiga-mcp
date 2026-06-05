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
        mock_client.api.patch.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": {"name": "Done"},
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, status="Done", session_id=session)
        payload = mock_client.api.patch.call_args.kwargs["json"]
        assert payload["status"] == 12
        assert payload["version"] == 3

    def test_reparent_resolves_story_ref(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        # user_stories.get_by_ref is unaffected by the bug — mock it directly.
        mock_client.api.user_stories.get_by_ref.return_value = {"id": 99, "ref": 44}
        mock_client.api.patch.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, story_ref=44, session_id=session)
        payload = mock_client.api.patch.call_args.kwargs["json"]
        assert payload["user_story"] == 99

    def test_sprint_move_supported(self, session, mock_client):
        mock_client.api.get.side_effect = self._api_get
        mock_client.list_resources.return_value = [{"id": 33, "name": "Sprint 2", "closed": False}]
        mock_client.api.patch.return_value = {
            "ref": 7,
            "subject": "T",
            "status_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_task("p", 7, sprint="Sprint 2", session_id=session)
        payload = mock_client.api.patch.call_args.kwargs["json"]
        assert payload["milestone"] == 33

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
        mock_client.api.patch.return_value = {
            "ref": 7,
            "status_extra_info": {"name": "Done"},
            "is_closed": True,
        }
        result = wf.set_task_status("p", 7, "Done", session_id=session)
        assert result["status"] == "Done"
        assert result["is_closed"] is True
        # Confirm uses task_statuses, not userstory_statuses.
        mock_client.list_resources.assert_called_once_with("task_statuses", project_id=1)
        # Confirm PATCH payload carries resolved status ID and version.
        patch_payload = mock_client.api.patch.call_args.kwargs["json"]
        assert patch_payload["status"] == 99
        assert patch_payload["version"] == 2


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


class TestUpdateIssue:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _issue(self):
        return {"id": 300, "ref": 12, "version": 4}

    def _api_get(self, path, params=None):
        if "/issues/by_ref" in str(path):
            return self._issue()
        return self._project()

    def test_subject_and_description_patched_directly(self, session, mock_client):
        """update_issue must bypass issues.edit() (fixed-signature) via direct PATCH."""
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        mock_client.api.get.return_value = self._project()
        mock_client.api.patch.return_value = {
            "ref": 12,
            "subject": "New subject",
            "status_extra_info": None,
            "priority_extra_info": None,
            "severity_extra_info": None,
            "type_extra_info": None,
            "assigned_to_extra_info": None,
            "is_blocked": False,
        }
        wf.update_issue("p", 12, subject="New subject", description="desc", session_id=session)
        payload = mock_client.api.patch.call_args.kwargs["json"]
        assert payload["subject"] == "New subject"
        assert payload["description"] == "desc"
        assert payload["version"] == 4

    def test_no_op_raises(self, session, mock_client):
        mock_client.api.issues.get_by_ref.return_value = self._issue()
        mock_client.api.get.return_value = self._project()
        with pytest.raises(ValueError, match="No fields to update"):
            wf.update_issue("p", 12, session_id=session)


class TestAddComment:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def test_task_comment_uses_direct_get(self, session, mock_client):
        """add_comment for entity_type=task must use direct GET (not tasks.get_by_ref)."""
        task = {"id": 200, "ref": 7, "version": 3}

        def api_get(path, params=None):
            if "/tasks/by_ref" in str(path):
                return task
            return self._project()

        mock_client.api.get.side_effect = api_get
        wf.add_comment("p", 7, "hello task", entity_type="task", session_id=session)

        # Verify by_ref was fetched via client.api.get, not tasks.get_by_ref
        get_calls = [str(c.args[0]) for c in mock_client.api.get.call_args_list]
        assert any("/tasks/by_ref" in p for p in get_calls)
        mock_client.api.tasks.get_by_ref.assert_not_called()

        # Verify comment was PATCHed to the correct task endpoint
        patch_call = mock_client.api.patch.call_args
        assert "/tasks/200" in patch_call.args[0]
        assert patch_call.kwargs["json"]["comment"] == "hello task"
        assert patch_call.kwargs["json"]["version"] == 3

    def test_story_comment_still_works(self, session, mock_client):
        """add_comment for entity_type=story continues to use the same direct GET path."""
        story = {"id": 50, "ref": 5, "version": 1}

        def api_get(path, params=None):
            if "/userstories/by_ref" in str(path):
                return story
            return self._project()

        mock_client.api.get.side_effect = api_get
        wf.add_comment("p", 5, "hello story", entity_type="story", session_id=session)

        patch_call = mock_client.api.patch.call_args
        assert "/userstories/50" in patch_call.args[0]
        assert patch_call.kwargs["json"]["comment"] == "hello story"


class TestAssignItem:
    def _project(self):
        return {"id": 1, "slug": "p", "name": "P"}

    def _api_get(self, entity_path):
        def side_effect(path, params=None):
            if entity_path in str(path) and "by_ref" in str(path):
                return {"id": 200, "ref": 7, "version": 2}
            return self._project()

        return side_effect

    def _mock_members(self, mock_client):
        mock_client.list_resources.return_value = [
            {"user": 42, "user_extra_info": {"username": "alice", "full_name_display": "Alice"}}
        ]

    def test_assign_task_uses_direct_get_and_patch(self, session, mock_client):
        """assign_item for entity_type=task must bypass tasks.get_by_ref and tasks.edit."""
        mock_client.api.get.side_effect = self._api_get("tasks")
        self._mock_members(mock_client)
        mock_client.api.patch.return_value = {
            "assigned_to_extra_info": {"full_name_display": "Alice"}
        }
        result = wf.assign_item("p", 7, "alice", entity_type="task", session_id=session)

        mock_client.api.tasks.get_by_ref.assert_not_called()
        mock_client.api.tasks.edit.assert_not_called()

        patch_call = mock_client.api.patch.call_args
        assert "/tasks/200" in patch_call.args[0]
        assert patch_call.kwargs["json"]["assigned_to"] == 42
        assert patch_call.kwargs["json"]["version"] == 2
        assert result["assigned_to"] == "Alice"

    def test_assign_issue_uses_direct_patch(self, session, mock_client):
        """assign_item for entity_type=issue must bypass issues.edit (fixed-signature)."""
        mock_client.api.get.side_effect = self._api_get("issues")
        self._mock_members(mock_client)
        mock_client.api.patch.return_value = {
            "assigned_to_extra_info": {"full_name_display": "Alice"}
        }
        result = wf.assign_item("p", 7, "alice", entity_type="issue", session_id=session)

        mock_client.api.issues.edit.assert_not_called()

        patch_call = mock_client.api.patch.call_args
        assert "/issues/200" in patch_call.args[0]
        assert patch_call.kwargs["json"]["assigned_to"] == 42
        assert result["assigned_to"] == "Alice"
