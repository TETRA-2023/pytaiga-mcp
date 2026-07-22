import asyncio
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

# Import the server module instead of specific functions
import src.server_full as src_server
from src.taiga_client import TaigaClientWrapper

# Test constants
TEST_HOST = "https://your-test-taiga-instance.com"
TEST_USERNAME = "test_user"
TEST_PASSWORD = "test_password"


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
    """Every full-server tool parameter must expose a typed JSON schema.

    Guards against reintroducing `Any` (or `List[Any]`) on a tool signature. The
    `kwargs`/`filters` params accept either a JSON object or a JSON string, so they
    resolve to a typed `anyOf`, never `{}`.
    """
    tools = asyncio.run(src_server.mcp.list_tools())
    assert tools, "expected the full server to expose tools"
    offenders = [
        f"{tool.name}.{name}: {prop}"
        for tool in tools
        for name, prop in (tool.inputSchema or {}).get("properties", {}).items()
        if name != "session_id" and not _schema_is_typed(prop)
    ]
    assert not offenders, "untyped (Any) tool params found:\n" + "\n".join(offenders)


# ─── Helper fixtures ──────────────────────────────────────────────────


class TestTaigaTools:
    @pytest.fixture
    def session_setup(self):
        """Create a session setup for testing"""
        session_id = str(uuid.uuid4())
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        src_server.active_sessions[session_id] = mock_client
        yield session_id, mock_client
        src_server.active_sessions.pop(session_id, None)

    # ─── Authentication tests ─────────────────────────────────────────

    def test_login(self):
        """Test the login functionality"""
        with patch.object(TaigaClientWrapper, "login", return_value=True):
            src_server.active_sessions.clear()
            result = src_server.login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)
            assert "session_id" in result
            assert result["session_id"] in src_server.active_sessions
            src_server.active_sessions.clear()

    def test_login_missing_host(self):
        """Test login raises error when host is missing."""
        with patch("src.server_full.settings") as mock_settings:
            mock_settings.host = None
            mock_settings.get_username_value.return_value = TEST_USERNAME
            mock_settings.get_password_value.return_value = TEST_PASSWORD
            with pytest.raises(ValueError, match="Host URL required"):
                src_server.login(None, TEST_USERNAME, TEST_PASSWORD)

    def test_login_missing_credentials(self):
        """Test login raises error when credentials are missing."""
        with patch("src.server_full.settings") as mock_settings:
            mock_settings.host = TEST_HOST
            mock_settings.get_username_value.return_value = None
            mock_settings.get_password_value.return_value = None
            with pytest.raises(ValueError, match="Credentials required"):
                src_server.login(TEST_HOST, None, None)

    def test_login_failure(self):
        """Test login raises error on authentication failure."""
        with patch.object(TaigaClientWrapper, "login", return_value=False):
            with pytest.raises(RuntimeError, match="unexpected server error occurred during login"):
                src_server.login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)

    # ─── Session management tests ─────────────────────────────────────

    def test_get_default_session_available(self, session_setup):
        """Test get_default_session when default session exists."""
        session_id, mock_client = session_setup
        src_server.active_sessions["default"] = mock_client
        try:
            result = src_server.get_default_session()
            assert result["status"] == "active"
            assert result["session_id"] == "default"
            assert result["auto_authenticated"] is True
        finally:
            src_server.active_sessions.pop("default", None)

    def test_get_default_session_unavailable(self):
        """Test get_default_session when no default session exists."""
        src_server.active_sessions.pop("default", None)
        result = src_server.get_default_session()
        assert result["status"] == "unavailable"

    def test_logout(self, session_setup):
        """Test logout removes session."""
        session_id, mock_client = session_setup
        result = src_server.logout(session_id)
        assert result["status"] == "logged_out"
        assert session_id not in src_server.active_sessions

    def test_logout_nonexistent_session(self):
        """Test logout with a non-existent session."""
        fake_id = str(uuid.uuid4())
        # Need a default session or it will raise ValueError
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        src_server.active_sessions["default"] = mock_client
        try:
            result = src_server.logout(fake_id)
            assert result["status"] == "session_not_found"
        finally:
            src_server.active_sessions.pop("default", None)

    def test_session_status_active(self, session_setup):
        """Test session_status for an active session."""
        session_id, mock_client = session_setup
        mock_client.api.users.get_me.return_value = {"username": "test_user"}
        result = src_server.session_status(session_id)
        assert result["status"] == "active"
        assert result["username"] == "test_user"

    def test_session_status_inactive(self):
        """Test session_status for a non-existent session."""
        fake_id = str(uuid.uuid4())
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        src_server.active_sessions["default"] = mock_client
        try:
            result = src_server.session_status(fake_id)
            assert result["status"] == "inactive"
            assert result["reason"] == "not_found"
        finally:
            src_server.active_sessions.pop("default", None)

    # ─── Helper function tests ────────────────────────────────────────

    def test_get_session_id_with_explicit(self, session_setup):
        """Test _get_session_id returns explicit session_id."""
        session_id, _ = session_setup
        assert src_server._get_session_id(session_id) == session_id

    def test_get_session_id_default(self, session_setup):
        """Test _get_session_id returns default when available."""
        _, mock_client = session_setup
        src_server.active_sessions["default"] = mock_client
        try:
            assert src_server._get_session_id(None) == "default"
        finally:
            src_server.active_sessions.pop("default", None)

    def test_get_session_id_raises_without_default(self):
        """Test _get_session_id raises ValueError when no default session."""
        src_server.active_sessions.clear()
        with pytest.raises(ValueError, match="No session_id provided"):
            src_server._get_session_id(None)

    def test_get_authenticated_client_invalid(self):
        """Test _get_authenticated_client raises for invalid session."""
        with pytest.raises(PermissionError, match="Invalid or expired session"):
            src_server._get_authenticated_client("nonexistent-session")

    def test_execute_taiga_operation_success(self):
        """Test _execute_taiga_operation returns result on success."""
        result = src_server._execute_taiga_operation("test_op", lambda: {"ok": True})
        assert result == {"ok": True}

    def test_execute_taiga_operation_runtime_error(self):
        """Test _execute_taiga_operation wraps unexpected errors."""

        def failing():
            raise Exception("something broke")

        with pytest.raises(RuntimeError, match="Server error in test_op"):
            src_server._execute_taiga_operation("test_op", failing)

    # ─── TaigaAPIError repair tests (issue #57) ───────────────────────

    @staticmethod
    def _make_taiga_api_error(status_code, payload, *, json_decode_error=False):
        """Build a TaigaAPIError mimicking the upstream init behaviour."""
        from pytaigaclient.exceptions import TaigaAPIError

        response = MagicMock()
        response.status_code = status_code
        if json_decode_error:
            import requests

            response.json.side_effect = requests.exceptions.JSONDecodeError("e", "doc", 0)
            response.text = payload
        else:
            response.json.return_value = payload
        return TaigaAPIError(status_code, response)

    def test_repair_taiga_api_error_drf_dict_list(self):
        """DRF-style {field: [msg, ...]} body should replace the placeholder detail."""
        err = self._make_taiga_api_error(
            400, {"milestone_id": ["This field is required.", "Must be int."]}
        )
        assert err.error_detail == "No error message provided by API."

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "milestone_id: This field is required.; Must be int."
        assert str(err) == "API Error 400: milestone_id: This field is required.; Must be int."

    def test_repair_taiga_api_error_drf_dict_scalar(self):
        """DRF-style {field: scalar} body should also be formatted, not stringified."""
        err = self._make_taiga_api_error(400, {"name": "already exists", "slug": "invalid"})

        src_server._repair_taiga_api_error(err)

        assert "name: already exists" in err.error_detail
        assert "slug: invalid" in err.error_detail
        assert str(err).startswith("API Error 400: ")

    def test_repair_taiga_api_error_legacy_format_left_alone(self):
        """Legacy {"_error_message": "..."} bodies are already handled upstream — don't touch."""
        err = self._make_taiga_api_error(400, {"_error_message": "Legacy message"})
        assert err.error_detail == "Legacy message"

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "Legacy message"
        assert str(err) == "API Error 400: Legacy message"

    def test_repair_taiga_api_error_non_json_body_left_alone(self):
        """Non-JSON bodies set error_detail from response.text upstream — don't touch."""
        err = self._make_taiga_api_error(500, "Internal Server Error", json_decode_error=True)
        assert err.error_detail == "Internal Server Error"

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "Internal Server Error"

    def test_repair_taiga_api_error_empty_dict_keeps_placeholder(self):
        """Empty {} body has no fields to extract — placeholder stays."""
        err = self._make_taiga_api_error(400, {})

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "No error message provided by API."

    def test_repair_taiga_api_error_no_response(self):
        """If e.response is None, no-op safely (defensive)."""
        err = self._make_taiga_api_error(400, {"will_not_be_read": "x"})
        assert err.error_detail == "No error message provided by API."
        err.response = None

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "No error message provided by API."

    def test_repair_taiga_api_error_json_raises_during_repair(self):
        """If response.json() raises during repair, no-op safely."""
        err = self._make_taiga_api_error(400, {"will_not_be_read": "x"})
        assert err.error_detail == "No error message provided by API."
        err.response.json.side_effect = ValueError("boom")

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == "No error message provided by API."

    def test_repair_taiga_api_error_nested_dict_value(self):
        """Nested non-scalar values are JSON-encoded, not Python-repr'd."""
        err = self._make_taiga_api_error(400, {"field": {"nested": "msg"}})

        src_server._repair_taiga_api_error(err)

        assert err.error_detail == 'field: {"nested": "msg"}'
        assert str(err) == 'API Error 400: field: {"nested": "msg"}'

    def test_execute_taiga_operation_repairs_drf_error(self):
        """Errors flowing through the wrapper get repaired before re-raise."""
        from pytaigaclient.exceptions import TaigaAPIError

        err = self._make_taiga_api_error(400, {"milestone_id": ["This field is required."]})

        def failing():
            raise err

        with pytest.raises(TaigaAPIError) as excinfo:
            src_server._execute_taiga_operation("test_op", failing)

        assert excinfo.value is err
        assert excinfo.value.error_detail == "milestone_id: This field is required."
        assert "milestone_id: This field is required." in str(excinfo.value)

    # ─── kwargs parsing and validation tests ──────────────────────────

    def test_parse_mcp_kwargs_empty(self):
        """Test parsing empty kwargs."""
        assert src_server._parse_mcp_kwargs({}) == {}

    def test_parse_mcp_kwargs_json_string(self):
        """Test parsing kwargs with JSON string."""
        result = src_server._parse_mcp_kwargs({"kwargs": '{"name": "test"}'})
        assert result == {"name": "test"}

    def test_parse_mcp_kwargs_dict(self):
        """Test parsing kwargs with dict value."""
        result = src_server._parse_mcp_kwargs({"kwargs": {"name": "test"}})
        assert result == {"name": "test"}

    def test_parse_mcp_kwargs_passthrough(self):
        """Test parsing kwargs with multiple keys passes through."""
        data = {"name": "test", "desc": "value"}
        assert src_server._parse_mcp_kwargs(data) == data

    def test_validate_kwargs_strips_unexpected(self):
        """Test _validate_kwargs strips unexpected fields."""
        result = src_server._validate_kwargs("project", {"name": "test", "invalid_field": "value"})
        assert result == {"name": "test"}

    def test_validate_kwargs_strict_raises(self):
        """Test _validate_kwargs raises in strict mode."""
        with pytest.raises(ValueError, match="Unexpected kwargs"):
            src_server._validate_kwargs(
                "project", {"name": "test", "invalid_field": "value"}, strict=True
            )

    def test_validate_kwargs_empty(self):
        """Test _validate_kwargs with empty dict."""
        assert src_server._validate_kwargs("project", {}) == {}

    def test_validate_kwargs_unknown_resource(self):
        """Test _validate_kwargs with unknown resource type passes through."""
        data = {"any": "field"}
        assert src_server._validate_kwargs("unknown_type", data) == data

    # ─── Project tools tests ─────────────────────────────────────────

    def test_list_projects(self, session_setup):
        """Test list_projects functionality"""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 123, "name": "Test Project"}]
        projects = src_server.list_projects(session_id)
        assert len(projects) == 1
        assert projects[0]["name"] == "Test Project"
        assert projects[0]["id"] == 123
        mock_client.list_resources.assert_called_once_with("projects")

    def test_list_all_projects(self, session_setup):
        """Test list_all_projects delegates to list_projects."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 1, "name": "P1"}]
        projects = src_server.list_all_projects(session_id)
        assert len(projects) == 1

    def test_get_project(self, session_setup):
        """Test get_project returns project by ID."""
        session_id, mock_client = session_setup
        mock_client.api.projects.get.return_value = {
            "id": 123,
            "name": "Test",
            "slug": "test",
            "version": 1,
        }
        result = src_server.get_project(123, session_id)
        assert result["id"] == 123
        mock_client.api.projects.get.assert_called_once_with(123)

    def test_get_project_by_slug(self, session_setup):
        """Test get_project_by_slug returns project by slug."""
        session_id, mock_client = session_setup
        mock_client.api.projects.get_by_slug.return_value = {
            "id": 123,
            "name": "Test",
            "slug": "test-slug",
            "version": 1,
        }
        result = src_server.get_project_by_slug("test-slug", session_id)
        assert result["slug"] == "test-slug"
        mock_client.api.projects.get_by_slug.assert_called_once_with(slug="test-slug")

    def test_create_project(self, session_setup):
        """Test create_project creates a project."""
        session_id, mock_client = session_setup
        mock_client.api.projects.create.return_value = {
            "id": 456,
            "name": "New Project",
            "slug": "new-project",
            "version": 1,
        }
        result = src_server.create_project("New Project", "A description", "{}", session_id)
        assert result["id"] == 456
        assert result["name"] == "New Project"
        mock_client.api.projects.create.assert_called_once_with(
            name="New Project", description="A description"
        )

    def test_create_project_with_kwargs(self, session_setup):
        """Test create_project with extra kwargs."""
        session_id, mock_client = session_setup
        mock_client.api.projects.create.return_value = {
            "id": 456,
            "name": "Private Project",
            "is_private": True,
            "version": 1,
        }
        result = src_server.create_project(
            "Private Project", "Desc", '{"is_private": true}', session_id
        )
        assert result["id"] == 456
        mock_client.api.projects.create.assert_called_once_with(
            name="Private Project", description="Desc", is_private=True
        )

    def test_create_project_empty_name(self, session_setup):
        """Test create_project raises error for empty name."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Project name and description are required"):
            src_server.create_project("", "desc", "{}", session_id)

    def test_update_project(self, session_setup):
        """Test update_project functionality with version."""
        session_id, mock_client = session_setup
        mock_client.api.projects.get.return_value = {"id": 123, "name": "Old Name", "version": 1}
        mock_client.api.projects.edit.return_value = {"id": 123, "name": "New Name", "version": 2}
        result = src_server.update_project(123, '{"name": "New Name"}', session_id)
        mock_client.api.projects.edit.assert_called_once_with(
            project_id=123, version=1, name="New Name"
        )
        assert result["name"] == "New Name"

    def test_update_project_without_version(self, session_setup):
        """Test update_project when project has no version field (Taiga projects)."""
        session_id, mock_client = session_setup
        mock_client.api.projects.get.return_value = {"id": 123, "name": "Old Name"}
        mock_client.api.projects.edit.return_value = {"id": 123, "name": "New Name"}
        result = src_server.update_project(123, '{"name": "New Name"}', session_id)
        mock_client.api.projects.edit.assert_called_once_with(
            project_id=123, version=None, name="New Name"
        )
        assert result["name"] == "New Name"

    def test_update_project_no_kwargs(self, session_setup):
        """Test update_project with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_project(123, "{}", session_id)
        mock_client.api.projects.get.assert_not_called()
        mock_client.api.projects.update.assert_not_called()

    def test_delete_project(self, session_setup):
        """Test delete_project."""
        session_id, mock_client = session_setup
        mock_client.api.projects.delete.return_value = None
        result = src_server.delete_project(123, session_id)
        assert result["status"] == "deleted"
        assert result["project_id"] == 123
        mock_client.api.projects.delete.assert_called_once_with(project_id=123)

    # ─── Project Tag Management tests ─────────────────────────────────

    def test_get_project_tags_colors(self, session_setup):
        """Test get_project_tags_colors returns tag-color mapping."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"bug": "#FF0000", "feature": "#00FF00"}

        result = src_server.get_project_tags_colors(21, session_id)

        mock_client.api.get.assert_called_once_with("/projects/21/tags_colors")
        assert result == {"bug": "#FF0000", "feature": "#00FF00"}

    def test_edit_project_tag_rename(self, session_setup):
        """Test edit_project_tag renames a tag."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.edit_project_tag(
            21, "old-name", new_tag="new-name", session_id=session_id
        )

        mock_client.api.post.assert_called_once_with(
            "/projects/21/edit_tag", json={"tag": "old-name", "new_tag": "new-name"}
        )
        assert result["status"] == "tag_updated"
        assert result["new_tag"] == "new-name"

    def test_edit_project_tag_recolor(self, session_setup):
        """Test edit_project_tag changes tag color."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.edit_project_tag(21, "bug", color="#FF0000", session_id=session_id)

        mock_client.api.post.assert_called_once_with(
            "/projects/21/edit_tag", json={"tag": "bug", "color": "#FF0000"}
        )
        assert result["status"] == "tag_updated"
        assert result["color"] == "#FF0000"

    def test_edit_project_tag_empty_name_raises(self, session_setup):
        """Test edit_project_tag raises ValueError for empty tag name."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Tag name cannot be empty"):
            src_server.edit_project_tag(21, "", color="#FF0000", session_id=session_id)

    def test_edit_project_tag_rename_and_recolor(self, session_setup):
        """Test edit_project_tag with both color and new_tag."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.edit_project_tag(
            21, "bug", color="#0000FF", new_tag="defect", session_id=session_id
        )

        mock_client.api.post.assert_called_once_with(
            "/projects/21/edit_tag",
            json={"tag": "bug", "color": "#0000FF", "new_tag": "defect"},
        )
        assert result["status"] == "tag_updated"
        assert result["color"] == "#0000FF"
        assert result["new_tag"] == "defect"

    def test_edit_project_tag_no_changes_raises(self, session_setup):
        """Test edit_project_tag raises ValueError when neither color nor new_tag provided."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="At least one of"):
            src_server.edit_project_tag(21, "bug", session_id=session_id)

    def test_mix_project_tags(self, session_setup):
        """Test mix_project_tags merges tags."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.mix_project_tags(21, ["bug", "defect"], "bug", session_id)

        mock_client.api.post.assert_called_once_with(
            "/projects/21/mix_tags", json={"from_tags": ["bug", "defect"], "to_tag": "bug"}
        )
        assert result["status"] == "tags_merged"
        assert result["from_tags"] == ["bug", "defect"]
        assert result["to_tag"] == "bug"

    def test_mix_project_tags_empty_to_tag_raises(self, session_setup):
        """Test mix_project_tags raises ValueError for empty target tag."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Target tag name"):
            src_server.mix_project_tags(21, ["bug"], "", session_id)

    def test_mix_project_tags_empty_from_tags_raises(self, session_setup):
        """Test mix_project_tags raises ValueError for empty from_tags list."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="from_tags"):
            src_server.mix_project_tags(21, [], "bug", session_id)

    def test_mix_project_tags_strips_whitespace(self, session_setup):
        """Test mix_project_tags strips whitespace from tag names."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        src_server.mix_project_tags(21, ["  bug  ", "defect", "  "], "  merged  ", session_id)

        mock_client.api.post.assert_called_once_with(
            "/projects/21/mix_tags", json={"from_tags": ["bug", "defect"], "to_tag": "merged"}
        )

    # ─── User Story tools tests ──────────────────────────────────────

    def test_list_user_stories(self, session_setup):
        """Test list_user_stories functionality"""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 456, "subject": "Test User Story"}]
        stories = src_server.list_user_stories(123, "{}", session_id)
        assert len(stories) == 1
        assert stories[0]["subject"] == "Test User Story"
        mock_client.list_resources.assert_called_once_with("user_stories", project_id=123)

    def test_list_user_stories_with_filters(self, session_setup):
        """Test list_user_stories with filters."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 1, "subject": "Filtered"}]
        src_server.list_user_stories(123, '{"status": 1}', session_id)
        mock_client.list_resources.assert_called_once_with("user_stories", project_id=123, status=1)

    def test_create_user_story(self, session_setup):
        """Test create_user_story functionality"""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.create.return_value = {"id": 456, "subject": "New Story"}
        story = src_server.create_user_story(
            123, "New Story", '{"description": "Test description"}', session_id
        )
        assert story["subject"] == "New Story"
        assert story["id"] == 456
        mock_client.api.user_stories.create.assert_called_once_with(
            project=123, subject="New Story", description="Test description"
        )

    def test_create_user_story_empty_subject(self, session_setup):
        """Test create_user_story raises for empty subject."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="User story subject cannot be empty"):
            src_server.create_user_story(123, "", "{}", session_id)

    def test_get_user_story(self, session_setup):
        """Test get_user_story returns story by ID."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get.return_value = {
            "id": 456,
            "ref": 1,
            "subject": "Story",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_user_story(456, session_id)
        assert result["id"] == 456
        mock_client.api.user_stories.get.assert_called_once_with(456)

    def test_get_user_story_by_ref(self, session_setup):
        """Test get_user_story_by_ref returns story by ref number."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get_by_ref.return_value = {
            "id": 456,
            "ref": 1,
            "subject": "Story",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_user_story_by_ref(123, 1, session_id)
        assert result["id"] == 456
        assert result["ref"] == 1
        mock_client.api.user_stories.get_by_ref.assert_called_once_with(ref=1, project=123)

    def test_get_user_story_by_ref_not_found(self, session_setup):
        """Test get_user_story_by_ref raises when ref not found."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get_by_ref.return_value = None
        with pytest.raises(ValueError, match="not found"):
            src_server.get_user_story_by_ref(123, 999, session_id)

    def test_update_user_story(self, session_setup):
        """Test update_user_story."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get.return_value = {
            "id": 456,
            "description": "Old desc",
            "version": 1,
        }
        mock_client.api.user_stories.edit.return_value = {
            "id": 456,
            "description": "New desc",
            "version": 2,
        }
        result = src_server.update_user_story(456, '{"description": "New desc"}', session_id)
        assert result["description"] == "New desc"
        mock_client.api.user_stories.edit.assert_called_once_with(
            user_story_id=456, version=1, description="New desc"
        )

    def test_update_user_story_no_kwargs(self, session_setup):
        """Test update_user_story with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_user_story(456, "{}", session_id)
        mock_client.api.user_stories.get.assert_not_called()
        mock_client.api.user_stories.edit.assert_not_called()

    def test_delete_user_story(self, session_setup):
        """Test delete_user_story."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.delete.return_value = None
        result = src_server.delete_user_story(456, session_id)
        assert result["status"] == "deleted"
        assert result["user_story_id"] == 456

    def test_assign_user_story_to_user(self, session_setup):
        """Test assign_user_story_to_user delegates to update."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get.return_value = {
            "id": 456,
            "assigned_to": None,
            "version": 1,
        }
        mock_client.api.user_stories.edit.return_value = {
            "id": 456,
            "assigned_to": 10,
            "version": 2,
        }
        src_server.assign_user_story_to_user(456, 10, session_id)
        mock_client.api.user_stories.edit.assert_called_once_with(
            user_story_id=456, version=1, assigned_to=10
        )

    def test_assign_user_story_to_user_by_username(self, session_setup):
        """assign_*_to_user resolves a username to an ID via the entity's project members."""
        session_id, mock_client = session_setup
        mock_client.get_resource.return_value = {"id": 456, "project": 1}
        mock_client.list_resources.return_value = [
            # pending-invite member with null fields first — must not crash (pytaiga-mcp#120)
            {"user": None, "email": None, "full_name": None, "user_extra_info": None},
            {
                "user": 10,
                "email": "bob@example.com",
                "full_name": "Bob Stone",
                "user_extra_info": {"username": "bob", "full_name_display": "Bob Stone"},
            },
        ]
        mock_client.api.user_stories.get.return_value = {
            "id": 456,
            "assigned_to": None,
            "version": 1,
        }
        mock_client.api.user_stories.edit.return_value = {
            "id": 456,
            "assigned_to": 10,
            "version": 2,
        }
        src_server.assign_user_story_to_user(456, "bob", session_id)
        mock_client.get_resource.assert_called_once_with("user_stories", 456)
        mock_client.api.user_stories.edit.assert_called_once_with(
            user_story_id=456, version=1, assigned_to=10
        )

    def test_unassign_user_story_from_user(self, session_setup):
        """Test unassign_user_story_from_user sets assigned_to to None."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get.return_value = {"id": 456, "assigned_to": 10, "version": 1}
        mock_client.api.user_stories.edit.return_value = {
            "id": 456,
            "assigned_to": None,
            "version": 2,
        }
        src_server.unassign_user_story_from_user(456, session_id)
        mock_client.api.user_stories.edit.assert_called_once_with(
            user_story_id=456, version=1, assigned_to=None
        )

    def test_get_user_story_statuses(self, session_setup):
        """Test get_user_story_statuses."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "New"},
            {"id": 2, "name": "In Progress"},
        ]
        result = src_server.get_user_story_statuses(123, session_id)
        assert len(result) == 2
        mock_client.list_resources.assert_called_once_with("userstory_statuses", project_id=123)

    # ─── Task tools tests ────────────────────────────────────────────

    def test_list_tasks(self, session_setup):
        """Test list_tasks functionality"""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 789, "subject": "Test Task"}]
        tasks = src_server.list_tasks(123, "{}", session_id)
        assert len(tasks) == 1
        assert tasks[0]["subject"] == "Test Task"
        mock_client.list_resources.assert_called_once_with("tasks", project_id=123)

    def test_list_tasks_with_filters(self, session_setup):
        """Test list_tasks with filters."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 1, "subject": "Filtered"}]
        src_server.list_tasks(123, '{"milestone": 5}', session_id)
        mock_client.list_resources.assert_called_once_with("tasks", project_id=123, milestone=5)

    def test_create_task(self, session_setup):
        """Test create_task."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.create.return_value = {
            "id": 789,
            "subject": "New Task",
            "project": 123,
        }
        result = src_server.create_task(123, "New Task", "{}", session_id)
        assert result["id"] == 789
        assert result["subject"] == "New Task"
        mock_client.api.tasks.create.assert_called_once_with(
            project=123, subject="New Task", data=None
        )

    def test_create_task_with_kwargs(self, session_setup):
        """Test create_task with extra kwargs."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.create.return_value = {"id": 789, "subject": "Task"}
        src_server.create_task(123, "Task", '{"description": "Some desc"}', session_id)
        mock_client.api.tasks.create.assert_called_once_with(
            project=123, subject="Task", data={"description": "Some desc"}
        )

    def test_create_task_empty_subject(self, session_setup):
        """Test create_task raises for empty subject."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Task subject cannot be empty"):
            src_server.create_task(123, "", "{}", session_id)

    def test_get_task(self, session_setup):
        """Test get_task returns task by ID."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.get.return_value = {
            "id": 789,
            "ref": 5,
            "subject": "Task",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_task(789, session_id)
        assert result["id"] == 789
        mock_client.api.tasks.get.assert_called_once_with(789)

    def test_get_task_by_ref(self, session_setup):
        """Test get_task_by_ref returns task by ref number via direct API call."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {
            "id": 789,
            "ref": 5,
            "subject": "Task",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_task_by_ref(123, 5, session_id)
        assert result["id"] == 789
        assert result["ref"] == 5
        mock_client.api.get.assert_called_once_with(
            "/tasks/by_ref", params={"ref": 5, "project": 123}
        )

    def test_get_task_by_ref_not_found(self, session_setup):
        """Test get_task_by_ref raises when ref not found."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            src_server.get_task_by_ref(123, 999, session_id)

    def test_update_task(self, session_setup):
        """Test update_task."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.get.return_value = {"id": 789, "description": "Old", "version": 1}
        mock_client.api.tasks.edit.return_value = {"id": 789, "description": "New", "version": 2}
        result = src_server.update_task(789, '{"description": "New"}', session_id)
        assert result["description"] == "New"
        mock_client.api.tasks.edit.assert_called_once_with(
            task_id=789, version=1, data={"description": "New"}
        )

    def test_update_task_no_kwargs(self, session_setup):
        """Test update_task with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_task(789, "{}", session_id)
        mock_client.api.tasks.get.assert_not_called()
        mock_client.api.tasks.edit.assert_not_called()

    def test_update_task_missing_version(self, session_setup):
        """Test update_task raises ValueError (not RuntimeError) when version is missing."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.get.return_value = {"id": 789, "subject": "x"}
        with pytest.raises(ValueError, match="Could not determine version"):
            src_server.update_task(789, '{"subject": "new"}', session_id)
        mock_client.api.tasks.edit.assert_not_called()

    def test_delete_task(self, session_setup):
        """Test delete_task."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.delete.return_value = None
        result = src_server.delete_task(789, session_id)
        assert result["status"] == "deleted"
        assert result["task_id"] == 789

    def test_assign_task_to_user(self, session_setup):
        """Test assign_task_to_user delegates to update_task."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.get.return_value = {"id": 789, "version": 1}
        mock_client.api.tasks.edit.return_value = {"id": 789, "assigned_to": 10, "version": 2}
        src_server.assign_task_to_user(789, 10, session_id)
        mock_client.api.tasks.edit.assert_called_once_with(
            task_id=789, version=1, data={"assigned_to": 10}
        )

    def test_unassign_task_from_user(self, session_setup):
        """Test unassign_task_from_user sets assigned_to to None."""
        session_id, mock_client = session_setup
        mock_client.api.tasks.get.return_value = {"id": 789, "version": 1}
        mock_client.api.tasks.edit.return_value = {"id": 789, "assigned_to": None, "version": 2}
        src_server.unassign_task_from_user(789, session_id)
        mock_client.api.tasks.edit.assert_called_once_with(
            task_id=789, version=1, data={"assigned_to": None}
        )

    # ─── Issue tools tests ───────────────────────────────────────────

    def test_list_issues(self, session_setup):
        """Test list_issues."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 100, "subject": "Bug"}]
        result = src_server.list_issues(123, "{}", session_id)
        assert len(result) == 1
        assert result[0]["subject"] == "Bug"
        mock_client.list_resources.assert_called_once_with("issues", project_id=123)

    def test_list_issues_with_filters(self, session_setup):
        """Test list_issues with filters."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = []
        src_server.list_issues(123, '{"priority": 3}', session_id)
        mock_client.list_resources.assert_called_once_with("issues", project_id=123, priority=3)

    def test_create_issue(self, session_setup):
        """Test create_issue."""
        session_id, mock_client = session_setup
        mock_client.api.issues.create.return_value = {
            "id": 100,
            "subject": "New Bug",
            "project": 123,
        }
        result = src_server.create_issue(
            project_id=123,
            subject="New Bug",
            priority_id=1,
            status_id=1,
            severity_id=1,
            type_id=1,
            kwargs="{}",
            session_id=session_id,
        )
        assert result["id"] == 100
        assert result["subject"] == "New Bug"
        mock_client.api.issues.create.assert_called_once_with(
            project=123,
            subject="New Bug",
            data={"priority": 1, "status": 1, "type": 1, "severity": 1},
        )

    def test_create_issue_empty_subject(self, session_setup):
        """Test create_issue raises for empty subject."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Issue subject cannot be empty"):
            src_server.create_issue(123, "", 1, 1, 1, 1, "{}", session_id)

    def test_get_issue(self, session_setup):
        """Test get_issue returns issue by ID."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get.return_value = {
            "id": 100,
            "ref": 10,
            "subject": "Bug",
            "status": 1,
            "priority": 1,
            "severity": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_issue(100, session_id)
        assert result["id"] == 100
        mock_client.api.issues.get.assert_called_once_with(100)

    def test_get_issue_by_ref(self, session_setup):
        """Test get_issue_by_ref returns issue by ref number."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get_by_ref.return_value = {
            "id": 100,
            "ref": 10,
            "subject": "Bug",
            "status": 1,
            "priority": 1,
            "severity": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_issue_by_ref(123, 10, session_id)
        assert result["id"] == 100
        assert result["ref"] == 10
        mock_client.api.issues.get_by_ref.assert_called_once_with(ref=10, project=123)

    def test_get_issue_by_ref_not_found(self, session_setup):
        """Test get_issue_by_ref raises when ref not found."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get_by_ref.return_value = {}
        with pytest.raises(ValueError, match="not found"):
            src_server.get_issue_by_ref(123, 999, session_id)

    def test_update_issue(self, session_setup):
        """Test update_issue."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get.return_value = {"id": 100, "description": "Old", "version": 1}
        mock_client.api.issues.edit.return_value = {"id": 100, "description": "New", "version": 2}
        result = src_server.update_issue(100, '{"description": "New"}', session_id)
        assert result["description"] == "New"
        mock_client.api.issues.edit.assert_called_once_with(
            issue_id=100, version=1, data={"description": "New"}
        )

    def test_update_issue_no_kwargs(self, session_setup):
        """Test update_issue with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_issue(100, "{}", session_id)
        mock_client.api.issues.get.assert_not_called()
        mock_client.api.issues.edit.assert_not_called()

    def test_update_issue_missing_version(self, session_setup):
        """Test update_issue raises ValueError (not RuntimeError) when version is missing."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get.return_value = {"id": 100, "subject": "x"}
        with pytest.raises(ValueError, match="Could not determine version"):
            src_server.update_issue(100, '{"subject": "new"}', session_id)
        mock_client.api.issues.edit.assert_not_called()

    def test_delete_issue(self, session_setup):
        """Test delete_issue."""
        session_id, mock_client = session_setup
        mock_client.api.issues.delete.return_value = None
        result = src_server.delete_issue(100, session_id)
        assert result["status"] == "deleted"
        assert result["issue_id"] == 100

    def test_assign_issue_to_user(self, session_setup):
        """Test assign_issue_to_user delegates to update_issue."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get.return_value = {"id": 100, "version": 1}
        mock_client.api.issues.edit.return_value = {"id": 100, "assigned_to": 10, "version": 2}
        src_server.assign_issue_to_user(100, 10, session_id)
        mock_client.api.issues.edit.assert_called_once_with(
            issue_id=100, version=1, data={"assigned_to": 10}
        )

    def test_unassign_issue_from_user(self, session_setup):
        """Test unassign_issue_from_user."""
        session_id, mock_client = session_setup
        mock_client.api.issues.get.return_value = {"id": 100, "version": 1}
        mock_client.api.issues.edit.return_value = {"id": 100, "assigned_to": None, "version": 2}
        src_server.unassign_issue_from_user(100, session_id)
        mock_client.api.issues.edit.assert_called_once_with(
            issue_id=100, version=1, data={"assigned_to": None}
        )

    def test_get_issue_statuses(self, session_setup):
        """Test get_issue_statuses."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "New"},
            {"id": 2, "name": "Closed"},
        ]
        result = src_server.get_issue_statuses(123, session_id)
        assert len(result) == 2
        mock_client.list_resources.assert_called_once_with("issue_statuses", project_id=123)

    def test_get_issue_priorities(self, session_setup):
        """Test get_issue_priorities."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "Low"},
            {"id": 2, "name": "Normal"},
            {"id": 3, "name": "High"},
        ]
        result = src_server.get_issue_priorities(123, session_id)
        assert len(result) == 3
        assert result[0]["name"] == "Low"
        mock_client.list_resources.assert_called_once_with("priorities", project_id=123)

    def test_get_issue_severities(self, session_setup):
        """Test get_issue_severities."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "Wishlist"},
            {"id": 2, "name": "Minor"},
            {"id": 3, "name": "Normal"},
        ]
        result = src_server.get_issue_severities(123, session_id)
        assert len(result) == 3
        assert result[0]["name"] == "Wishlist"
        mock_client.list_resources.assert_called_once_with("severities", project_id=123)

    def test_get_issue_types(self, session_setup):
        """Test get_issue_types."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "Bug"},
            {"id": 2, "name": "Enhancement"},
        ]
        result = src_server.get_issue_types(123, session_id)
        assert len(result) == 2
        mock_client.list_resources.assert_called_once_with("issue_types", project_id=123)

    # ─── Project Configuration CRUD tests ────────────────────────────

    def test_create_project_config_status(self, session_setup):
        """Test create_project_config for issue_status."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 10, "name": "In Review"}
        result = src_server.create_project_config(
            21, "issue_status", "In Review", session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/issue-statuses",
            json={"project": 21, "name": "In Review", "color": "#999999"},
        )
        assert result["name"] == "In Review"

    def test_create_project_config_with_options(self, session_setup):
        """Test create_project_config with all optional fields."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 11}
        src_server.create_project_config(
            21,
            "task_status",
            "Done",
            color="#00FF00",
            is_closed=True,
            order=5,
            session_id=session_id,
        )
        call_json = mock_client.api.post.call_args[1]["json"]
        assert call_json["name"] == "Done"
        assert call_json["color"] == "#00FF00"
        assert call_json["is_closed"] is True
        assert call_json["order"] == 5

    def test_create_project_config_priority(self, session_setup):
        """Test create_project_config for priority."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 12}
        src_server.create_project_config(21, "priority", "Urgent", session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/priorities",
            json={"project": 21, "name": "Urgent", "color": "#999999"},
        )

    def test_create_project_config_severity(self, session_setup):
        """Test create_project_config for severity."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 13}
        src_server.create_project_config(21, "severity", "Critical", session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/severities",
            json={"project": 21, "name": "Critical", "color": "#999999"},
        )

    def test_create_project_config_issue_type(self, session_setup):
        """Test create_project_config for issue_type."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 14}
        src_server.create_project_config(21, "issue_type", "Security", session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/issue-types",
            json={"project": 21, "name": "Security", "color": "#999999"},
        )

    def test_create_project_config_epic_status(self, session_setup):
        """Test create_project_config for epic_status."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 15}
        src_server.create_project_config(21, "epic_status", "Backlog", session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/epic-statuses",
            json={"project": 21, "name": "Backlog", "color": "#999999"},
        )

    def test_create_project_config_empty_name_raises(self):
        """Test create_project_config raises on empty name."""
        with pytest.raises(ValueError, match="Name cannot be empty"):
            src_server.create_project_config(21, "issue_status", "  ")

    def test_create_project_config_invalid_type_raises(self):
        """Test create_project_config raises on invalid config_type."""
        with pytest.raises(ValueError, match="Invalid config_type"):
            src_server.create_project_config(21, "bogus", "Name")

    def test_create_project_config_user_story_status_alias(self, session_setup):
        """Test user_story_status alias maps to userstory-statuses."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 16}
        src_server.create_project_config(21, "user_story_status", "New", session_id=session_id)
        assert mock_client.api.post.call_args[0][0] == "/userstory-statuses"

    def test_update_project_config(self, session_setup):
        """Test update_project_config calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 10, "name": "Reviewed"}
        result = src_server.update_project_config(
            10, "issue_status", name="Reviewed", session_id=session_id
        )
        mock_client.api.patch.assert_called_once_with(
            "/issue-statuses/10", json={"name": "Reviewed"}
        )
        assert result["name"] == "Reviewed"

    def test_update_project_config_multiple_fields(self, session_setup):
        """Test update_project_config with multiple fields."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 10}
        src_server.update_project_config(
            10, "priority", name="Critical", color="#FF0000", order=1, session_id=session_id
        )
        call_json = mock_client.api.patch.call_args[1]["json"]
        assert call_json["name"] == "Critical"
        assert call_json["color"] == "#FF0000"
        assert call_json["order"] == 1

    def test_update_project_config_empty_name_raises(self):
        """Test update_project_config raises on empty name."""
        with pytest.raises(ValueError, match="Name cannot be empty"):
            src_server.update_project_config(10, "issue_status", name="  ")

    def test_update_project_config_no_fields_raises(self):
        """Test update_project_config raises when no fields provided."""
        with pytest.raises(ValueError, match="At least one field"):
            src_server.update_project_config(10, "issue_status")

    def test_update_project_config_invalid_type_raises(self):
        """Test update_project_config raises on invalid config_type."""
        with pytest.raises(ValueError, match="Invalid config_type"):
            src_server.update_project_config(10, "bogus", name="X")

    def test_delete_project_config(self, session_setup):
        """Test delete_project_config calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None
        result = src_server.delete_project_config(10, "severity", session_id=session_id)
        mock_client.api.delete.assert_called_once_with("/severities/10")
        assert result["status"] == "deleted"
        assert result["item_id"] == 10

    def test_delete_project_config_invalid_type_raises(self):
        """Test delete_project_config raises on invalid config_type."""
        with pytest.raises(ValueError, match="Invalid config_type"):
            src_server.delete_project_config(10, "bogus")

    def test_bulk_update_order_project_config(self, session_setup):
        """Test bulk_update_order_project_config calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        result = src_server.bulk_update_order_project_config(
            21, "issue_status", [[1, 1], [2, 2], [3, 3]], session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/issue-statuses/bulk_update_order",
            json={"project": 21, "bulk_orders": [[1, 1], [2, 2], [3, 3]]},
        )
        assert result["status"] == "updated"
        assert result["items_reordered"] == 3

    def test_bulk_update_order_project_config_empty_raises(self):
        """Test bulk_update_order_project_config raises on empty list."""
        with pytest.raises(ValueError, match="bulk_orders cannot be empty"):
            src_server.bulk_update_order_project_config(21, "priority", [])

    def test_bulk_update_order_project_config_invalid_pair_raises(self):
        """Test bulk_update_order_project_config raises on malformed pair."""
        with pytest.raises(ValueError, match="must be a \\[id, order\\] pair"):
            src_server.bulk_update_order_project_config(21, "priority", [[1]])

    def test_bulk_update_order_project_config_invalid_type_raises(self):
        """Test bulk_update_order_project_config raises on invalid config_type."""
        with pytest.raises(ValueError, match="Invalid config_type"):
            src_server.bulk_update_order_project_config(21, "bogus", [[1, 1]])

    # ─── Story Points tools tests ─────────────────────────────────────

    def test_list_points(self, session_setup):
        """Test list_points returns point values for a project."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "1", "value": 1.0},
            {"id": 2, "name": "3", "value": 3.0},
        ]

        result = src_server.list_points(21, session_id)

        assert len(result) == 2
        mock_client.list_resources.assert_called_once_with("points", project_id=21)

    def test_create_point(self, session_setup):
        """Test create_point creates a new point value."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 10, "name": "5", "value": 5.0}

        result = src_server.create_point(21, "5", value=5.0, session_id=session_id)

        mock_client.api.post.assert_called_once_with(
            "/points", json={"project": 21, "name": "5", "value": 5.0}
        )
        assert result["name"] == "5"

    def test_create_point_without_value(self, session_setup):
        """Test create_point without numeric value (e.g., '?' point)."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 11, "name": "?"}

        src_server.create_point(21, "?", session_id=session_id)

        mock_client.api.post.assert_called_once_with("/points", json={"project": 21, "name": "?"})

    def test_create_point_empty_name_raises(self, session_setup):
        """Test create_point raises ValueError for empty name."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Point name cannot be empty"):
            src_server.create_point(21, "", session_id=session_id)

    def test_update_point(self, session_setup):
        """Test update_point updates a point value."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 10, "name": "8", "value": 8.0}

        result = src_server.update_point(10, name="8", value=8.0, session_id=session_id)

        mock_client.api.patch.assert_called_once_with(
            "/points/10", json={"name": "8", "value": 8.0}
        )
        assert result["name"] == "8"

    def test_update_point_no_changes_raises(self, session_setup):
        """Test update_point raises ValueError when no fields provided."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="At least one of"):
            src_server.update_point(10, session_id=session_id)

    def test_update_point_empty_name_raises(self, session_setup):
        """Test update_point raises ValueError for empty name."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Point name cannot be empty"):
            src_server.update_point(10, name="  ", session_id=session_id)

    def test_delete_point(self, session_setup):
        """Test delete_point deletes a point value."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None

        result = src_server.delete_point(10, session_id)

        mock_client.api.delete.assert_called_once_with("/points/10")
        assert result["status"] == "deleted"
        assert result["point_id"] == 10

    # ─── Custom Attributes tests ─────────────────────────────────────

    def test_list_custom_attributes(self, session_setup):
        """Test list_custom_attributes calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [{"id": 1, "name": "Patent Number"}]
        result = src_server.list_custom_attributes(21, "user_story", session_id=session_id)
        mock_client.api.get.assert_called_once_with(
            "/userstory-custom-attributes", params={"project": 21}
        )
        assert len(result) == 1

    def test_list_custom_attributes_issue(self, session_setup):
        """Test list_custom_attributes for issues."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []
        src_server.list_custom_attributes(21, "issue", session_id=session_id)
        mock_client.api.get.assert_called_once_with(
            "/issue-custom-attributes", params={"project": 21}
        )

    def test_list_custom_attributes_invalid_type_raises(self):
        """Test list_custom_attributes raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.list_custom_attributes(21, "invalid")

    def test_create_custom_attribute(self, session_setup):
        """Test create_custom_attribute calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 1, "name": "Patent Number", "type": "text"}
        result = src_server.create_custom_attribute(
            21, "task", "Patent Number", session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/task-custom-attributes",
            json={"project": 21, "name": "Patent Number", "type": "text"},
        )
        assert result["name"] == "Patent Number"

    def test_create_custom_attribute_with_options(self, session_setup):
        """Test create_custom_attribute with all optional fields."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {"id": 1}
        src_server.create_custom_attribute(
            21,
            "epic",
            "Status",
            attr_type="dropdown",
            description="Workflow status",
            order=5,
            extra="Option A,Option B",
            session_id=session_id,
        )
        call_json = mock_client.api.post.call_args[1]["json"]
        assert call_json["type"] == "dropdown"
        assert call_json["description"] == "Workflow status"
        assert call_json["order"] == 5
        assert call_json["extra"] == "Option A,Option B"

    def test_create_custom_attribute_empty_name_raises(self):
        """Test create_custom_attribute raises on empty name."""
        with pytest.raises(ValueError, match="Attribute name cannot be empty"):
            src_server.create_custom_attribute(21, "task", "  ")

    def test_create_custom_attribute_invalid_type_raises(self):
        """Test create_custom_attribute raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.create_custom_attribute(21, "bogus", "Name")

    def test_update_custom_attribute(self, session_setup):
        """Test update_custom_attribute calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 5, "name": "New Name"}
        result = src_server.update_custom_attribute(
            5, "user_story", name="New Name", session_id=session_id
        )
        mock_client.api.patch.assert_called_once_with(
            "/userstory-custom-attributes/5", json={"name": "New Name"}
        )
        assert result["name"] == "New Name"

    def test_update_custom_attribute_no_fields_raises(self):
        """Test update_custom_attribute raises when no fields provided."""
        with pytest.raises(ValueError, match="At least one field"):
            src_server.update_custom_attribute(5, "task")

    def test_update_custom_attribute_empty_name_raises(self):
        """Test update_custom_attribute raises on empty name."""
        with pytest.raises(ValueError, match="Attribute name cannot be empty"):
            src_server.update_custom_attribute(5, "task", name="  ")

    def test_delete_custom_attribute(self, session_setup):
        """Test delete_custom_attribute calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None
        result = src_server.delete_custom_attribute(5, "issue", session_id=session_id)
        mock_client.api.delete.assert_called_once_with("/issue-custom-attributes/5")
        assert result["status"] == "deleted"
        assert result["attribute_id"] == 5

    def test_delete_custom_attribute_invalid_type_raises(self):
        """Test delete_custom_attribute raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.delete_custom_attribute(5, "bogus")

    def test_get_custom_attribute_values(self, session_setup):
        """Test get_custom_attribute_values calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {
            "attributes_values": {"1": "US2024/001234", "2": "USPTO"},
            "version": 3,
        }
        result = src_server.get_custom_attribute_values(100, "user_story", session_id=session_id)
        mock_client.api.get.assert_called_once_with("/userstories/custom-attributes-values/100")
        assert result["version"] == 3
        assert result["attributes_values"]["1"] == "US2024/001234"

    def test_get_custom_attribute_values_epic(self, session_setup):
        """Test get_custom_attribute_values for epics."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"attributes_values": {}, "version": 1}
        src_server.get_custom_attribute_values(50, "epic", session_id=session_id)
        mock_client.api.get.assert_called_once_with("/epics/custom-attributes-values/50")

    def test_get_custom_attribute_values_invalid_type_raises(self):
        """Test get_custom_attribute_values raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.get_custom_attribute_values(100, "bogus")

    def test_set_custom_attribute_values(self, session_setup):
        """Test set_custom_attribute_values calls correct endpoint with version."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {
            "attributes_values": {"1": "EP2024/5678"},
            "version": 4,
        }
        result = src_server.set_custom_attribute_values(
            100, "task", {"1": "EP2024/5678"}, version=3, session_id=session_id
        )
        mock_client.api.patch.assert_called_once_with(
            "/tasks/custom-attributes-values/100",
            json={"attributes_values": {"1": "EP2024/5678"}, "version": 3},
        )
        assert result["version"] == 4

    def test_set_custom_attribute_values_empty_raises(self):
        """Test set_custom_attribute_values raises on empty attributes_values."""
        with pytest.raises(ValueError, match="attributes_values cannot be empty"):
            src_server.set_custom_attribute_values(100, "task", {}, version=1)

    def test_set_custom_attribute_values_invalid_type_raises(self):
        """Test set_custom_attribute_values raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.set_custom_attribute_values(100, "bogus", {"1": "x"}, version=1)

    def test_custom_attributes_userstory_alias(self, session_setup):
        """Test that 'userstory' alias works same as 'user_story'."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []
        src_server.list_custom_attributes(21, "userstory", session_id=session_id)
        mock_client.api.get.assert_called_once_with(
            "/userstory-custom-attributes", params={"project": 21}
        )

    # ─── Attachment tests ────────────────────────────────────────────

    def test_list_attachments(self, session_setup):
        """Test list_attachments calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [{"id": 1, "name": "doc.pdf"}]
        result = src_server.list_attachments(100, "user_story", session_id=session_id)
        mock_client.api.get.assert_called_once_with(
            "/userstories/attachments", params={"object_id": 100}
        )
        assert len(result) == 1

    def test_list_attachments_with_project(self, session_setup):
        """Test list_attachments passes project_id when provided."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []
        src_server.list_attachments(100, "task", project_id=21, session_id=session_id)
        mock_client.api.get.assert_called_once_with(
            "/tasks/attachments", params={"object_id": 100, "project": 21}
        )

    def test_list_attachments_wiki(self, session_setup):
        """Test list_attachments for wiki entity type."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []
        src_server.list_attachments(10, "wiki", session_id=session_id)
        mock_client.api.get.assert_called_once_with("/wiki/attachments", params={"object_id": 10})

    def test_list_attachments_invalid_type_raises(self):
        """Test list_attachments raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.list_attachments(100, "bogus")

    def test_get_attachment(self, session_setup):
        """Test get_attachment calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 5, "name": "spec.pdf", "url": "https://..."}
        result = src_server.get_attachment(5, "issue", session_id=session_id)
        mock_client.api.get.assert_called_once_with("/issues/attachments/5")
        assert result["id"] == 5

    def test_get_attachment_invalid_type_raises(self):
        """Test get_attachment raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.get_attachment(5, "bogus")

    def test_create_attachment(self, session_setup, tmp_path):
        """Test create_attachment uploads file via multipart."""
        session_id, mock_client = session_setup
        # Create a temporary file to upload
        test_file = tmp_path / "test.pdf"
        test_file.write_text("fake pdf content")
        mock_client.api.post.return_value = {"id": 10, "name": "test.pdf"}

        result = src_server.create_attachment(
            21, 100, "epic", str(test_file), session_id=session_id
        )
        assert result["id"] == 10
        call_kwargs = mock_client.api.post.call_args
        assert call_kwargs[0][0] == "/epics/attachments"
        assert call_kwargs[1]["data"]["project"] == "21"
        assert call_kwargs[1]["data"]["object_id"] == "100"
        # files should contain the attached_file tuple
        assert "attached_file" in call_kwargs[1]["files"]

    def test_create_attachment_with_description(self, session_setup, tmp_path):
        """Test create_attachment passes description."""
        session_id, mock_client = session_setup
        test_file = tmp_path / "notes.txt"
        test_file.write_text("notes")
        mock_client.api.post.return_value = {"id": 11}

        src_server.create_attachment(
            21, 50, "task", str(test_file), description="Design notes", session_id=session_id
        )
        call_data = mock_client.api.post.call_args[1]["data"]
        assert call_data["description"] == "Design notes"

    def test_create_attachment_empty_path_raises(self):
        """Test create_attachment raises on empty file_path."""
        with pytest.raises(ValueError, match="file_path cannot be empty"):
            src_server.create_attachment(21, 100, "task", "  ")

    def test_create_attachment_file_not_found_raises(self):
        """Test create_attachment raises when file doesn't exist."""
        with pytest.raises(ValueError, match="File not found"):
            src_server.create_attachment(21, 100, "task", "/nonexistent/file.pdf")

    def test_create_attachment_invalid_type_raises(self):
        """Test create_attachment raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.create_attachment(21, 100, "bogus", "/some/file.pdf")

    def test_update_attachment(self, session_setup):
        """Test update_attachment calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 5, "description": "Updated"}
        result = src_server.update_attachment(
            5, "user_story", description="Updated", session_id=session_id
        )
        mock_client.api.patch.assert_called_once_with(
            "/userstories/attachments/5", json={"description": "Updated"}
        )
        assert result["description"] == "Updated"

    def test_update_attachment_deprecated(self, session_setup):
        """Test update_attachment with is_deprecated flag."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 5, "is_deprecated": True}
        src_server.update_attachment(5, "issue", is_deprecated=True, session_id=session_id)
        mock_client.api.patch.assert_called_once_with(
            "/issues/attachments/5", json={"is_deprecated": True}
        )

    def test_update_attachment_no_fields_raises(self):
        """Test update_attachment raises when no fields provided."""
        with pytest.raises(ValueError, match="At least one field"):
            src_server.update_attachment(5, "task")

    def test_update_attachment_invalid_type_raises(self):
        """Test update_attachment raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.update_attachment(5, "bogus", description="x")

    def test_delete_attachment(self, session_setup):
        """Test delete_attachment calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None
        result = src_server.delete_attachment(5, "wiki", session_id=session_id)
        mock_client.api.delete.assert_called_once_with("/wiki/attachments/5")
        assert result["status"] == "deleted"
        assert result["attachment_id"] == 5

    def test_delete_attachment_invalid_type_raises(self):
        """Test delete_attachment raises on invalid entity_type."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            src_server.delete_attachment(5, "bogus")

    def test_attachments_wiki_page_alias(self, session_setup):
        """Test that 'wiki_page' alias works same as 'wiki'."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []
        src_server.list_attachments(10, "wiki_page", session_id=session_id)
        mock_client.api.get.assert_called_once_with("/wiki/attachments", params={"object_id": 10})

    # ─── Epic tools tests ────────────────────────────────────────────

    def test_list_epics(self, session_setup):
        """Test list_epics."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 200, "subject": "Epic 1"}]
        result = src_server.list_epics(123, "{}", session_id)
        assert len(result) == 1
        assert result[0]["subject"] == "Epic 1"
        mock_client.list_resources.assert_called_once_with("epics", project_id=123)

    def test_list_epics_with_filters(self, session_setup):
        """Test list_epics with filters."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = []
        src_server.list_epics(123, '{"status": 2}', session_id)
        mock_client.list_resources.assert_called_once_with("epics", project_id=123, status=2)

    def test_create_epic(self, session_setup):
        """Test create_epic."""
        session_id, mock_client = session_setup
        mock_client.api.epics.create.return_value = {
            "id": 200,
            "subject": "New Epic",
            "project": 123,
        }
        result = src_server.create_epic(123, "New Epic", "{}", session_id)
        assert result["id"] == 200
        mock_client.api.epics.create.assert_called_once_with(project=123, subject="New Epic")

    def test_create_epic_with_kwargs(self, session_setup):
        """Test create_epic with extra kwargs."""
        session_id, mock_client = session_setup
        mock_client.api.epics.create.return_value = {"id": 200, "subject": "Epic"}
        src_server.create_epic(123, "Epic", '{"color": "#FF0000"}', session_id)
        mock_client.api.epics.create.assert_called_once_with(
            project=123, subject="Epic", color="#FF0000"
        )

    def test_create_epic_empty_subject(self, session_setup):
        """Test create_epic raises for empty subject."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Epic subject cannot be empty"):
            src_server.create_epic(123, "", "{}", session_id)

    def test_get_epic(self, session_setup):
        """Test get_epic returns epic by ID."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get.return_value = {
            "id": 200,
            "ref": 1,
            "subject": "Epic",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_epic(200, session_id)
        assert result["id"] == 200
        mock_client.api.epics.get.assert_called_once_with(200)

    def test_get_epic_by_ref(self, session_setup):
        """Test get_epic_by_ref returns epic by ref number."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get_by_ref.return_value = {
            "id": 200,
            "ref": 1,
            "subject": "Epic",
            "status": 1,
            "project": 123,
            "version": 1,
        }
        result = src_server.get_epic_by_ref(123, 1, session_id)
        assert result["id"] == 200
        assert result["ref"] == 1
        mock_client.api.epics.get_by_ref.assert_called_once_with(ref=1, project=123)

    def test_get_epic_by_ref_not_found(self, session_setup):
        """Test get_epic_by_ref raises when ref not found."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get_by_ref.return_value = {}
        with pytest.raises(ValueError, match="not found"):
            src_server.get_epic_by_ref(123, 999, session_id)

    def test_update_epic(self, session_setup):
        """Test update_epic."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get.return_value = {"id": 200, "description": "Old", "version": 1}
        mock_client.api.epics.edit.return_value = {"id": 200, "description": "New", "version": 2}
        result = src_server.update_epic(200, '{"description": "New"}', session_id)
        assert result["description"] == "New"
        mock_client.api.epics.edit.assert_called_once_with(
            epic_id=200, version=1, description="New"
        )

    def test_update_epic_no_kwargs(self, session_setup):
        """Test update_epic with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_epic(200, "{}", session_id)
        mock_client.api.epics.get.assert_not_called()
        mock_client.api.epics.edit.assert_not_called()

    def test_update_epic_missing_version(self, session_setup):
        """Test update_epic raises ValueError (not RuntimeError) when version is missing."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get.return_value = {"id": 200, "subject": "x"}
        with pytest.raises(ValueError, match="Could not determine version"):
            src_server.update_epic(200, '{"subject": "new"}', session_id)
        mock_client.api.epics.edit.assert_not_called()

    def test_delete_epic(self, session_setup):
        """Test delete_epic."""
        session_id, mock_client = session_setup
        mock_client.api.epics.delete.return_value = None
        result = src_server.delete_epic(200, session_id)
        assert result["status"] == "deleted"
        assert result["epic_id"] == 200

    def test_assign_epic_to_user(self, session_setup):
        """Test assign_epic_to_user delegates to update_epic."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get.return_value = {"id": 200, "version": 1}
        mock_client.api.epics.edit.return_value = {"id": 200, "assigned_to": 10, "version": 2}
        src_server.assign_epic_to_user(200, 10, session_id)
        mock_client.api.epics.edit.assert_called_once_with(epic_id=200, version=1, assigned_to=10)

    def test_unassign_epic_from_user(self, session_setup):
        """Test unassign_epic_from_user."""
        session_id, mock_client = session_setup
        mock_client.api.epics.get.return_value = {"id": 200, "version": 1}
        mock_client.api.epics.edit.return_value = {"id": 200, "assigned_to": None, "version": 2}
        src_server.unassign_epic_from_user(200, session_id)
        mock_client.api.epics.edit.assert_called_once_with(epic_id=200, version=1, assigned_to=None)

    # ─── Milestone tools tests ───────────────────────────────────────

    def test_list_milestones(self, session_setup):
        """Test list_milestones."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 300, "name": "Sprint 1", "slug": "sprint-1", "project": 123}
        ]
        result = src_server.list_milestones(123, session_id)
        assert len(result) == 1
        assert result[0]["name"] == "Sprint 1"
        mock_client.list_resources.assert_called_once_with("milestones", project_id=123)

    def test_create_milestone(self, session_setup):
        """Test create_milestone."""
        session_id, mock_client = session_setup
        mock_client.api.milestones.create.return_value = {
            "id": 300,
            "name": "Sprint 1",
            "project": 123,
        }
        result = src_server.create_milestone(
            123, "Sprint 1", "2025-01-01", "2025-01-14", session_id
        )
        assert result["id"] == 300
        mock_client.api.milestones.create.assert_called_once_with(
            project=123,
            name="Sprint 1",
            estimated_start="2025-01-01",
            estimated_finish="2025-01-14",
        )

    def test_create_milestone_missing_fields(self, session_setup):
        """Test create_milestone raises when required fields are missing."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Milestone requires"):
            src_server.create_milestone(123, "", "2025-01-01", "2025-01-14", session_id)

    def test_get_milestone(self, session_setup):
        """Test get_milestone."""
        session_id, mock_client = session_setup
        mock_client.api.milestones.get.return_value = {
            "id": 300,
            "name": "Sprint 1",
            "slug": "sprint-1",
            "project": 123,
            "version": 1,
        }
        result = src_server.get_milestone(300, session_id)
        assert result["id"] == 300
        mock_client.api.milestones.get.assert_called_once_with(300)

    def test_update_milestone(self, session_setup):
        """Test update_milestone."""
        session_id, mock_client = session_setup
        mock_client.api.milestones.get.return_value = {"id": 300, "name": "Sprint 1", "version": 1}
        mock_client.api.milestones.edit.return_value = {
            "id": 300,
            "name": "Sprint 1 Updated",
            "version": 2,
        }
        result = src_server.update_milestone(300, '{"name": "Sprint 1 Updated"}', session_id)
        assert result["name"] == "Sprint 1 Updated"
        mock_client.api.milestones.edit.assert_called_once_with(
            milestone_id=300, version=1, name="Sprint 1 Updated"
        )

    def test_update_milestone_no_kwargs(self, session_setup):
        """Test update_milestone with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_milestone(300, "{}", session_id)
        mock_client.api.milestones.get.assert_not_called()
        mock_client.api.milestones.edit.assert_not_called()

    def test_delete_milestone(self, session_setup):
        """Test delete_milestone."""
        session_id, mock_client = session_setup
        mock_client.api.milestones.delete.return_value = None
        result = src_server.delete_milestone(300, session_id)
        assert result["status"] == "deleted"
        assert result["milestone_id"] == 300

    # ─── Swimlane tools tests (issue #24) ─────────────────────────────

    def test_list_swimlanes(self, session_setup):
        """Test list_swimlanes routes through list_resources('swimlanes', ...)."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "Security", "order": 100, "project": 9}
        ]
        result = src_server.list_swimlanes(9, session_id)
        assert len(result) == 1
        assert result[0]["name"] == "Security"
        mock_client.list_resources.assert_called_once_with("swimlanes", project_id=9)

    def test_create_swimlane(self, session_setup):
        """Test create_swimlane POSTs project + name to /swimlanes."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = {
            "id": 1,
            "name": "Security",
            "project": 9,
            "order": 100,
        }
        result = src_server.create_swimlane(9, "Security", session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/swimlanes", json={"project": 9, "name": "Security"}
        )
        assert result["id"] == 1
        assert result["name"] == "Security"

    def test_create_swimlane_empty_name_raises(self, session_setup):
        """Test create_swimlane raises on empty name before any API call."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="Swimlane requires a non-empty name"):
            src_server.create_swimlane(9, "", session_id=session_id)
        mock_client.api.post.assert_not_called()

    def test_get_swimlane(self, session_setup):
        """Test get_swimlane GETs /swimlanes/{id}."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 1, "name": "Security", "project": 9, "order": 100}
        result = src_server.get_swimlane(1, session_id)
        mock_client.api.get.assert_called_once_with("/swimlanes/1")
        assert result["id"] == 1

    def test_update_swimlane(self, session_setup):
        """Test update_swimlane PATCHes /swimlanes/{id} with parsed kwargs (no version handling)."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {
            "id": 1,
            "name": "Security renamed",
            "project": 9,
            "order": 100,
        }
        result = src_server.update_swimlane(1, '{"name": "Security renamed"}', session_id)
        mock_client.api.patch.assert_called_once_with(
            "/swimlanes/1", json={"name": "Security renamed"}
        )
        assert result["name"] == "Security renamed"

    def test_update_swimlane_no_kwargs_raises(self, session_setup):
        """Test update_swimlane with no kwargs raises ValueError before any API call."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_swimlane(1, "{}", session_id)
        mock_client.api.patch.assert_not_called()

    def test_update_swimlane_strips_unknown_kwargs(self, session_setup):
        """Test update_swimlane drops kwargs not in ALLOWED_KWARGS['swimlane']."""
        session_id, mock_client = session_setup
        mock_client.api.patch.return_value = {"id": 1, "name": "Security", "project": 9, "order": 5}
        src_server.update_swimlane(
            1, '{"name": "Security", "statuses": "ignored", "project": "ignored"}', session_id
        )
        sent = mock_client.api.patch.call_args[1]["json"]
        assert sent == {"name": "Security"}, f"unexpected payload: {sent}"

    def test_delete_swimlane(self, session_setup):
        """Test delete_swimlane DELETEs /swimlanes/{id} with params=None when move_to absent."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None
        result = src_server.delete_swimlane(1, session_id=session_id)
        mock_client.api.delete.assert_called_once_with("/swimlanes/1", params=None)
        assert result["status"] == "deleted"
        assert result["swimlane_id"] == 1
        assert result["moved_to"] is None

    def test_delete_swimlane_with_move_to(self, session_setup):
        """Test delete_swimlane sends params={'moveTo': ...} when migrating user stories."""
        session_id, mock_client = session_setup
        mock_client.api.delete.return_value = None
        result = src_server.delete_swimlane(1, move_to=2, session_id=session_id)
        mock_client.api.delete.assert_called_once_with("/swimlanes/1", params={"moveTo": 2})
        assert result["status"] == "deleted"
        assert result["swimlane_id"] == 1
        assert result["moved_to"] == 2

    def test_delete_swimlane_move_to_same_id_raises(self, session_setup):
        """Test delete_swimlane rejects move_to == swimlane_id before any API call."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="move_to .* cannot be the same as swimlane_id"):
            src_server.delete_swimlane(1, move_to=1, session_id=session_id)
        mock_client.api.delete.assert_not_called()

    def test_user_story_swimlane_kwarg_passes_through(self, session_setup):
        """Adding 'swimlane' to ALLOWED_KWARGS['user_story'] enables update_user_story assignment."""
        session_id, mock_client = session_setup
        mock_client.api.user_stories.get.return_value = {"id": 805, "version": 3}
        mock_client.api.user_stories.edit.return_value = {"id": 805, "swimlane": 1, "version": 4}
        src_server.update_user_story(805, '{"swimlane": 1}', session_id)
        mock_client.api.user_stories.edit.assert_called_once_with(
            user_story_id=805, version=3, swimlane=1
        )

    # ─── User management tools tests ─────────────────────────────────

    def test_get_project_members(self, session_setup):
        """Test get_project_members."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "user": 10, "full_name": "John Doe", "role_name": "Admin"}
        ]
        result = src_server.get_project_members(123, session_id)
        assert len(result) == 1
        assert result[0]["full_name"] == "John Doe"
        mock_client.list_resources.assert_called_once_with("memberships", project_id=123)

    def test_invite_project_user(self, session_setup):
        """Test invite_project_user."""
        session_id, mock_client = session_setup
        mock_client.api.memberships.invite.return_value = {
            "id": 50,
            "email": "user@test.com",
            "role": 5,
        }
        result = src_server.invite_project_user(123, "user@test.com", 5, session_id)
        assert result["email"] == "user@test.com"
        mock_client.api.memberships.invite.assert_called_once_with(
            project=123, email="user@test.com", role_id=5
        )

    def test_invite_project_user_empty_email(self, session_setup):
        """Test invite_project_user raises for empty email."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Email cannot be empty"):
            src_server.invite_project_user(123, "", 5, session_id)

    # ─── Wiki tools tests ────────────────────────────────────────────

    def test_list_wiki_pages(self, session_setup):
        """Test list_wiki_pages."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [{"id": 400, "slug": "home", "project": 123}]
        result = src_server.list_wiki_pages(123, session_id)
        assert len(result) == 1
        assert result[0]["slug"] == "home"
        mock_client.list_resources.assert_called_once_with("wiki", project_id=123)

    def test_get_wiki_page(self, session_setup):
        """Test get_wiki_page."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.get.return_value = {
            "id": 400,
            "slug": "home",
            "content": "# Welcome",
            "project": 123,
            "version": 1,
        }
        result = src_server.get_wiki_page(400, session_id)
        assert result["id"] == 400
        assert result["slug"] == "home"
        mock_client.api.wiki.get.assert_called_once_with(400)

    def test_get_wiki_page_by_slug(self, session_setup):
        """Test get_wiki_page_by_slug."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.get_by_slug.return_value = {
            "id": 400,
            "slug": "home",
            "content": "# Welcome",
            "project": 123,
            "version": 1,
        }
        result = src_server.get_wiki_page_by_slug(123, "home", session_id)
        assert result["id"] == 400
        assert result["slug"] == "home"
        mock_client.api.wiki.get_by_slug.assert_called_once_with(slug="home", project=123)

    def test_get_wiki_page_by_slug_not_found(self, session_setup):
        """Test get_wiki_page_by_slug raises ValueError when not found."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.get_by_slug.return_value = {}
        with pytest.raises(ValueError, match="not found"):
            src_server.get_wiki_page_by_slug(123, "nonexistent", session_id)

    def test_update_wiki_page(self, session_setup):
        """Test update_wiki_page."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.get.return_value = {
            "id": 400,
            "slug": "home",
            "content": "# Welcome",
            "project": 123,
            "version": 1,
        }
        mock_client.api.wiki.edit.return_value = {
            "id": 400,
            "slug": "home",
            "content": "# Updated",
            "project": 123,
            "version": 2,
        }
        result = src_server.update_wiki_page(400, json.dumps({"content": "# Updated"}), session_id)
        assert result["content"] == "# Updated"
        assert result["version"] == 2
        mock_client.api.wiki.edit.assert_called_once_with(
            wiki_page_id=400, version=1, data={"content": "# Updated"}
        )

    def test_update_wiki_page_no_kwargs(self, session_setup):
        """Test update_wiki_page with no kwargs raises ValueError (caller bug)."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="no fields to update"):
            src_server.update_wiki_page(400, None, session_id)
        mock_client.api.wiki.get.assert_not_called()
        mock_client.api.wiki.edit.assert_not_called()

    def test_update_wiki_page_missing_version(self, session_setup):
        """Test update_wiki_page raises ValueError (not RuntimeError) when version is missing."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.get.return_value = {"id": 400, "slug": "home", "content": "x"}
        with pytest.raises(ValueError, match="Could not determine version"):
            src_server.update_wiki_page(400, '{"content": "new"}', session_id)
        mock_client.api.wiki.edit.assert_not_called()

    def test_delete_wiki_page(self, session_setup):
        """Test delete_wiki_page."""
        session_id, mock_client = session_setup
        mock_client.api.wiki.delete.return_value = None
        result = src_server.delete_wiki_page(400, session_id)
        assert result["status"] == "deleted"
        assert result["wiki_page_id"] == 400
        mock_client.api.wiki.delete.assert_called_once_with(wiki_page_id=400)

    # ─── Verbosity tests for various tools ───────────────────────────

    def test_list_projects_verbosity_minimal(self, session_setup):
        """Test list_projects with minimal verbosity."""
        session_id, mock_client = session_setup
        mock_client.list_resources.return_value = [
            {"id": 1, "name": "P1", "slug": "p1", "description": "Long desc", "version": 1}
        ]
        result = src_server.list_projects(session_id, verbosity="minimal")
        assert result == [{"id": 1, "name": "P1", "slug": "p1"}]

    def test_get_project_verbosity_full(self, session_setup):
        """Test get_project with full verbosity returns all fields."""
        session_id, mock_client = session_setup
        full_data = {"id": 1, "name": "P1", "extra": "value", "version": 1}
        mock_client.api.projects.get.return_value = full_data
        result = src_server.get_project(1, session_id, verbosity="full")
        assert result == full_data

    # ─── Comment tests ─────────────────────────────────────────────────

    def test_add_comment(self, session_setup):
        """Test add_comment on an issue."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 42, "version": 3}
        mock_client.api.patch.return_value = {"id": 42, "version": 4}

        result = src_server.add_comment(42, "issue", "Test comment", session_id)

        mock_client.api.get.assert_called_once_with("/issues/42")
        mock_client.api.patch.assert_called_once_with(
            "/issues/42", json={"comment": "Test comment", "version": 3}
        )
        assert result == {
            "status": "comment_added",
            "object_type": "issue",
            "object_id": 42,
        }

    def test_add_comment_user_story(self, session_setup):
        """Test add_comment with user_story alias uses /userstories/ path."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 10, "version": 1}
        mock_client.api.patch.return_value = {"id": 10, "version": 2}

        result = src_server.add_comment(10, "user_story", "A comment", session_id)

        mock_client.api.get.assert_called_once_with("/userstories/10")
        mock_client.api.patch.assert_called_once_with(
            "/userstories/10", json={"comment": "A comment", "version": 1}
        )
        assert result["status"] == "comment_added"

    def test_add_comment_invalid_type(self, session_setup):
        """Test add_comment raises ValueError for invalid object_type."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="Invalid object_type"):
            src_server.add_comment(1, "invalid_type", "comment", session_id)

    def test_add_comment_unescapes_newlines_and_tabs(self, session_setup):
        """Test add_comment converts literal \\n and \\t to actual characters."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 42, "version": 3}
        mock_client.api.patch.return_value = {"id": 42, "version": 4}

        src_server.add_comment(42, "issue", "Line 1\\nLine 2\\tindented", session_id)

        mock_client.api.patch.assert_called_once_with(
            "/issues/42", json={"comment": "Line 1\nLine 2\tindented", "version": 3}
        )

    def test_edit_comment_unescapes_newlines_and_tabs(self, session_setup):
        """Test edit_comment converts literal \\n and \\t to actual characters."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        src_server.edit_comment(42, "issue", "abc", "Line 1\\nLine 2\\tindented", session_id)

        mock_client.api.post.assert_called_once_with(
            "/history/issue/42/edit_comment?id=abc",
            json={"comment": "Line 1\nLine 2\tindented"},
        )

    def test_add_comment_missing_version(self, session_setup):
        """Test add_comment raises ValueError when object has no version field."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {"id": 42}

        with pytest.raises(ValueError, match="Could not determine version"):
            src_server.add_comment(42, "issue", "Test comment", session_id)

    def test_list_comments(self, session_setup):
        """Test list_comments filters history to non-empty comments."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [
            {
                "id": "abc",
                "comment": "First comment",
                "comment_html": "<p>First comment</p>",
                "user": {"id": 1, "name": "User1"},
                "created_at": "2026-01-01T00:00:00Z",
                "delete_comment_date": None,
            },
            {
                "id": "def",
                "comment": "",
                "user": {"id": 1, "name": "User1"},
                "created_at": "2026-01-02T00:00:00Z",
            },
            {
                "id": "mno",
                "comment": "   ",
                "user": {"id": 1, "name": "User1"},
                "created_at": "2026-01-02T01:00:00Z",
            },
            {
                "id": "ghi",
                "comment": "Second comment",
                "comment_html": "<p>Second comment</p>",
                "user": {"id": 2, "name": "User2"},
                "created_at": "2026-01-03T00:00:00Z",
                "delete_comment_date": None,
            },
            {
                "id": "jkl",
                "comment": "Deleted comment",
                "comment_html": "<p>Deleted comment</p>",
                "user": {"id": 1, "name": "User1"},
                "created_at": "2026-01-04T00:00:00Z",
                "delete_comment_date": "2026-01-04T01:00:00Z",
            },
        ]

        result = src_server.list_comments(42, "issue", session_id)

        mock_client.api.get.assert_called_once_with("/history/issue/42")
        assert len(result) == 2
        assert result[0]["comment"] == "First comment"
        assert result[1]["comment"] == "Second comment"
        assert "delete_comment_date" not in result[0]

    def test_list_comments_userstory_alias(self, session_setup):
        """Test list_comments with 'userstory' input uses /history/userstory/ path."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []

        src_server.list_comments(5, "userstory", session_id)

        mock_client.api.get.assert_called_once_with("/history/userstory/5")

    def test_list_comments_invalid_type(self, session_setup):
        """Test list_comments raises ValueError for invalid object_type."""
        session_id, mock_client = session_setup
        with pytest.raises(ValueError, match="Invalid object_type"):
            src_server.list_comments(1, "invalid_type", session_id)

    def test_list_comments_empty_history(self, session_setup):
        """Test list_comments returns empty list for empty history."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []

        result = src_server.list_comments(1, "task", session_id)

        assert result == []

    # ─── Comment Management tests ──────────────────────────────────────

    def test_edit_comment(self, session_setup):
        """Test edit_comment posts to the correct history endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.edit_comment(42, "issue", "abc123", "Updated text", session_id)

        mock_client.api.post.assert_called_once_with(
            "/history/issue/42/edit_comment?id=abc123",
            json={"comment": "Updated text"},
        )
        assert result["status"] == "comment_edited"
        assert result["comment_id"] == "abc123"

    def test_edit_comment_strips_text(self, session_setup):
        """Test edit_comment strips whitespace from new comment text."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        src_server.edit_comment(42, "task", "abc", "  trimmed  ", session_id)

        mock_client.api.post.assert_called_once_with(
            "/history/task/42/edit_comment?id=abc",
            json={"comment": "trimmed"},
        )

    def test_edit_comment_empty_text_raises(self, session_setup):
        """Test edit_comment raises ValueError for empty new comment."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="New comment text must not be empty"):
            src_server.edit_comment(42, "issue", "abc", "", session_id)

    def test_edit_comment_invalid_type_raises(self, session_setup):
        """Test edit_comment raises ValueError for invalid object_type."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Invalid object_type"):
            src_server.edit_comment(42, "invalid", "abc", "text", session_id)

    def test_delete_comment(self, session_setup):
        """Test delete_comment posts to the correct history endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.delete_comment(42, "user_story", "abc123", session_id)

        mock_client.api.post.assert_called_once_with(
            "/history/userstory/42/delete_comment?id=abc123",
        )
        assert result["status"] == "comment_deleted"
        assert result["comment_id"] == "abc123"

    def test_delete_comment_empty_id_raises(self, session_setup):
        """Test delete_comment raises ValueError for empty comment_id."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Comment ID must not be empty"):
            src_server.delete_comment(42, "issue", "", session_id)

    def test_undelete_comment(self, session_setup):
        """Test undelete_comment posts to the correct history endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None

        result = src_server.undelete_comment(42, "epic", "abc123", session_id)

        mock_client.api.post.assert_called_once_with(
            "/history/epic/42/undelete_comment?id=abc123",
        )
        assert result["status"] == "comment_restored"
        assert result["comment_id"] == "abc123"

    def test_undelete_comment_invalid_type_raises(self, session_setup):
        """Test undelete_comment raises ValueError for invalid object_type."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Invalid object_type"):
            src_server.undelete_comment(42, "wiki", "abc", session_id)

    def test_get_comment_versions(self, session_setup):
        """Test get_comment_versions returns version history."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [
            {"date": "2026-01-01T00:00:00Z", "comment": "v1"},
            {"date": "2026-01-02T00:00:00Z", "comment": "v2"},
        ]

        result = src_server.get_comment_versions(42, "task", "abc123", session_id)

        mock_client.api.get.assert_called_once_with("/history/task/42/comment_versions?id=abc123")
        assert result["comment_id"] == "abc123"
        assert len(result["versions"]) == 2

    def test_get_comment_versions_empty_id_raises(self, session_setup):
        """Test get_comment_versions raises ValueError for empty comment_id."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Comment ID must not be empty"):
            src_server.get_comment_versions(42, "issue", "", session_id)

    # ─── History / Audit Trail tests ───────────────────────────────────

    def test_get_history(self, session_setup):
        """Test get_history returns full change history."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [
            {"id": "a", "type": 1, "values_diff": {"status": ["New", "In progress"]}},
            {"id": "b", "type": 1, "comment": "Some comment"},
        ]

        result = src_server.get_history(42, "issue", session_id)

        mock_client.api.get.assert_called_once_with("/history/issue/42")
        assert result["object_type"] == "issue"
        assert result["object_id"] == 42
        assert len(result["history"]) == 2

    def test_get_history_wiki(self, session_setup):
        """Test get_history works with wiki type."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []

        result = src_server.get_history(10, "wiki", session_id)

        mock_client.api.get.assert_called_once_with("/history/wiki/10")
        assert result["history"] == []

    def test_get_history_wiki_page_alias(self, session_setup):
        """Test get_history maps wiki_page to wiki path."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = []

        src_server.get_history(10, "wiki_page", session_id)

        mock_client.api.get.assert_called_once_with("/history/wiki/10")

    def test_get_history_user_story(self, session_setup):
        """Test get_history maps user_story to userstory path."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = [{"id": "x"}]

        result = src_server.get_history(5, "user_story", session_id)

        mock_client.api.get.assert_called_once_with("/history/userstory/5")
        assert len(result["history"]) == 1

    def test_get_history_invalid_type_raises(self, session_setup):
        """Test get_history raises ValueError for invalid object_type."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Invalid object_type"):
            src_server.get_history(42, "project", session_id)

    def test_get_history_no_session_raises(self):
        """Test get_history raises ValueError when no session available."""
        src_server.active_sessions.clear()
        with pytest.raises(ValueError, match="No session_id provided"):
            src_server.get_history(42, "issue")

    # ─── Login default session tests (PR: fix/login-default-session-and-slug) ──

    def test_login_sets_default_session_when_none_exists(self):
        """Test that login() sets the default session when no default exists."""
        with patch.object(TaigaClientWrapper, "login", return_value=True):
            src_server.active_sessions.clear()
            result = src_server.login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)
            assert "session_id" in result
            # Default session should have been set
            assert src_server.DEFAULT_SESSION_ID in src_server.active_sessions
            # The default session wrapper should be the same object as the new session's
            new_session_wrapper = src_server.active_sessions[result["session_id"]]
            default_wrapper = src_server.active_sessions[src_server.DEFAULT_SESSION_ID]
            assert new_session_wrapper is default_wrapper
            src_server.active_sessions.clear()

    def test_login_does_not_overwrite_existing_default_session(self):
        """Test that login() does NOT overwrite an existing default session."""
        existing_default = MagicMock()
        existing_default.is_authenticated = True
        src_server.active_sessions.clear()
        src_server.active_sessions[src_server.DEFAULT_SESSION_ID] = existing_default

        with patch.object(TaigaClientWrapper, "login", return_value=True):
            result = src_server.login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)
            assert "session_id" in result
            # Default session should still be the original one
            assert src_server.active_sessions[src_server.DEFAULT_SESSION_ID] is existing_default
            src_server.active_sessions.clear()

    # ─── Search tests ──────────────────────────────────────────────────

    def test_search_project(self, session_setup):
        """Test search_project returns structured results from Taiga search API."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {
            "count": 3,
            "userstories": [{"id": 1, "ref": 10, "subject": "US match"}],
            "tasks": [{"id": 2, "ref": 20, "subject": "Task match"}],
            "issues": [{"id": 3, "ref": 30, "subject": "Issue match"}],
            "wikipages": [],
            "epics": [],
        }

        result = src_server.search_project(21, "match", session_id)

        mock_client.api.get.assert_called_once_with(
            "/search", params={"project": 21, "text": "match"}
        )
        assert result["count"] == 3
        assert len(result["userstories"]) == 1
        assert len(result["tasks"]) == 1
        assert len(result["issues"]) == 1
        assert result["wikipages"] == []
        assert result["epics"] == []

    def test_search_project_empty_text_raises(self, session_setup):
        """Test search_project raises ValueError for empty search text."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Search text cannot be empty"):
            src_server.search_project(21, "", session_id)

    def test_search_project_whitespace_text_raises(self, session_setup):
        """Test search_project raises ValueError for whitespace-only search text."""
        session_id, _ = session_setup
        with pytest.raises(ValueError, match="Search text cannot be empty"):
            src_server.search_project(21, "   ", session_id)

    def test_search_project_strips_text(self, session_setup):
        """Test search_project strips whitespace from search text."""
        session_id, mock_client = session_setup
        mock_client.api.get.return_value = {
            "count": 0,
            "userstories": [],
            "tasks": [],
            "issues": [],
            "wikipages": [],
            "epics": [],
        }

        src_server.search_project(21, "  hello  ", session_id)

        mock_client.api.get.assert_called_once_with(
            "/search", params={"project": 21, "text": "hello"}
        )

    def test_search_project_no_session_raises(self):
        """Test search_project raises ValueError when no session available."""
        src_server.active_sessions.clear()
        with pytest.raises(ValueError, match="No session_id provided"):
            src_server.search_project(21, "query")

    # ─── Bulk Operations tests ────────────────────────────────────────

    def test_bulk_create_user_stories(self, session_setup):
        """Test bulk_create_user_stories calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [
            {"id": 1, "subject": "Story A"},
            {"id": 2, "subject": "Story B"},
        ]
        result = src_server.bulk_create_user_stories(
            21, ["Story A", "Story B"], session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_create",
            json={"project_id": 21, "bulk_stories": "Story A\nStory B"},
        )
        assert len(result) == 2

    def test_bulk_create_user_stories_empty_raises(self):
        """Test bulk_create_user_stories raises on empty list before checking session."""
        with pytest.raises(ValueError, match="Subjects list cannot be empty"):
            src_server.bulk_create_user_stories(21, [])

    def test_bulk_create_user_stories_whitespace_only_raises(self):
        """Test bulk_create_user_stories raises when all subjects are whitespace."""
        with pytest.raises(ValueError, match="only empty strings"):
            src_server.bulk_create_user_stories(21, ["  ", "", " "])

    def test_bulk_create_user_stories_strips_subjects(self, session_setup):
        """Test bulk_create_user_stories strips whitespace from subjects."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "subject": "Story A"}]
        src_server.bulk_create_user_stories(21, ["  Story A  "], session_id=session_id)
        call_json = mock_client.api.post.call_args[1]["json"]
        assert call_json["bulk_stories"] == "Story A"

    def test_bulk_create_tasks(self, session_setup):
        """Test bulk_create_tasks calls correct endpoint with required milestone_id."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "subject": "Task A"}]
        result = src_server.bulk_create_tasks(
            21, ["Task A"], milestone_id=89, session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/tasks/bulk_create",
            json={"project_id": 21, "bulk_tasks": "Task A", "milestone_id": 89},
        )
        assert len(result) == 1

    def test_bulk_create_tasks_with_user_story(self, session_setup):
        """Test bulk_create_tasks includes us_id when user_story_id provided."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "subject": "Task A"}]
        src_server.bulk_create_tasks(
            21, ["Task A"], milestone_id=89, user_story_id=5, session_id=session_id
        )
        call_json = mock_client.api.post.call_args[1]["json"]
        assert call_json["us_id"] == 5
        assert call_json["milestone_id"] == 89

    def test_bulk_create_tasks_empty_raises(self):
        """Test bulk_create_tasks raises on empty list before checking session."""
        with pytest.raises(ValueError, match="Subjects list cannot be empty"):
            src_server.bulk_create_tasks(21, [], milestone_id=89)

    def test_bulk_create_tasks_missing_milestone_raises(self):
        """Test bulk_create_tasks raises a Kanban-aware ValueError when milestone_id missing (#55)."""
        with pytest.raises(ValueError, match="milestone_id is required"):
            src_server.bulk_create_tasks(21, ["Task A"])

    def test_bulk_create_tasks_missing_milestone_mentions_kanban_workaround(self):
        """The missing-milestone error must point Kanban-only callers at create_task."""
        with pytest.raises(ValueError, match="create_task"):
            src_server.bulk_create_tasks(21, ["Task A"])

    def test_bulk_create_issues(self, session_setup):
        """Test bulk_create_issues calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "subject": "Issue A"}]
        result = src_server.bulk_create_issues(21, ["Issue A"], session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/issues/bulk_create",
            json={"project_id": 21, "bulk_issues": "Issue A"},
        )
        assert len(result) == 1

    def test_bulk_create_issues_empty_raises(self):
        """Test bulk_create_issues raises on empty list before checking session."""
        with pytest.raises(ValueError, match="Subjects list cannot be empty"):
            src_server.bulk_create_issues(21, [])

    def test_bulk_create_epics(self, session_setup):
        """Test bulk_create_epics calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "subject": "Epic A"}]
        result = src_server.bulk_create_epics(21, ["Epic A"], session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/epics/bulk_create",
            json={"project_id": 21, "bulk_epics": "Epic A"},
        )
        assert len(result) == 1

    def test_bulk_create_epics_empty_raises(self):
        """Test bulk_create_epics raises on empty list before checking session."""
        with pytest.raises(ValueError, match="Subjects list cannot be empty"):
            src_server.bulk_create_epics(21, [])

    def test_bulk_update_user_story_milestone(self, session_setup):
        """Test bulk_update_user_story_milestone calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        stories = [{"us_id": 1, "order": 1}, {"us_id": 2, "order": 2}]
        result = src_server.bulk_update_user_story_milestone(21, 5, stories, session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_milestone",
            json={"project_id": 21, "milestone_id": 5, "bulk_stories": stories},
        )
        assert result["status"] == "updated"
        assert result["stories_moved"] == 2

    def test_bulk_update_user_story_milestone_empty_raises(self):
        """Test bulk_update_user_story_milestone raises on empty list before checking session."""
        with pytest.raises(ValueError, match="bulk_stories list cannot be empty"):
            src_server.bulk_update_user_story_milestone(21, 5, [])

    def test_bulk_update_user_story_swimlane(self, session_setup):
        """Test bulk_update_user_story_swimlane POSTs to bulk_update_kanban_order with swimlane_id."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [
            {"id": 805, "swimlane": 1, "status": 97, "kanban_order": 1},
            {"id": 806, "swimlane": 1, "status": 97, "kanban_order": 2},
        ]
        result = src_server.bulk_update_user_story_swimlane(
            project_id=9,
            status_id=97,
            swimlane_id=1,
            user_story_ids=[805, 806],
            session_id=session_id,
        )
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_kanban_order",
            json={
                "project_id": 9,
                "status_id": 97,
                "swimlane_id": 1,
                "bulk_userstories": [805, 806],
            },
        )
        assert result["status"] == "updated"
        assert result["stories_moved"] == 2
        assert result["swimlane_id"] == 1
        assert result["status_id"] == 97

    def test_bulk_update_user_story_swimlane_empty_raises(self):
        """Test bulk_update_user_story_swimlane raises on empty list before any API call."""
        with pytest.raises(ValueError, match="user_story_ids list cannot be empty"):
            src_server.bulk_update_user_story_swimlane(
                project_id=9, status_id=97, swimlane_id=1, user_story_ids=[]
            )

    def test_bulk_update_user_story_order_backlog(self, session_setup):
        """Test bulk_update_user_story_order with backlog order type."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        stories = [{"us_id": 1, "order": 10}]
        result = src_server.bulk_update_user_story_order(
            21, "backlog", stories, session_id=session_id
        )
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_backlog_order",
            json={"project_id": 21, "bulk_stories": stories},
        )
        assert result["status"] == "reordered"
        assert result["order_type"] == "backlog"

    def test_bulk_update_user_story_order_kanban(self, session_setup):
        """Test bulk_update_user_story_order with kanban order type."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        stories = [{"us_id": 1, "order": 10}]
        src_server.bulk_update_user_story_order(21, "kanban", stories, session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_kanban_order",
            json={"project_id": 21, "bulk_stories": stories},
        )

    def test_bulk_update_user_story_order_sprint(self, session_setup):
        """Test bulk_update_user_story_order with sprint order type."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        stories = [{"us_id": 1, "order": 10}]
        src_server.bulk_update_user_story_order(21, "sprint", stories, session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/userstories/bulk_update_sprint_order",
            json={"project_id": 21, "bulk_stories": stories},
        )

    def test_bulk_update_user_story_order_invalid_type_raises(self):
        """Test bulk_update_user_story_order raises on invalid order type before session."""
        with pytest.raises(ValueError, match="Invalid order_type"):
            src_server.bulk_update_user_story_order(21, "invalid", [{"us_id": 1, "order": 1}])

    def test_bulk_update_user_story_order_empty_raises(self):
        """Test bulk_update_user_story_order raises on empty list before session."""
        with pytest.raises(ValueError, match="bulk_stories list cannot be empty"):
            src_server.bulk_update_user_story_order(21, "backlog", [])

    def test_bulk_create_memberships(self, session_setup):
        """Test bulk_create_memberships calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1, "email": "a@b.com"}]
        members = [{"email": "a@b.com", "role_id": 3}]
        result = src_server.bulk_create_memberships(21, members, session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/memberships/bulk_create",
            json={"project_id": 21, "bulk_memberships": members},
        )
        assert result["status"] == "invited"
        assert result["members_invited"] == [{"id": 1, "email": "a@b.com"}]

    def test_bulk_create_memberships_empty_raises(self):
        """Test bulk_create_memberships raises on empty list before checking session."""
        with pytest.raises(ValueError, match="Members list cannot be empty"):
            src_server.bulk_create_memberships(21, [])

    def test_bulk_create_memberships_missing_fields_raises(self):
        """Test bulk_create_memberships raises when member dict missing required fields."""
        with pytest.raises(ValueError, match="must have 'email' and 'role_id'"):
            src_server.bulk_create_memberships(21, [{"email": "a@b.com"}])

    def test_bulk_create_memberships_role_id_zero_accepted(self, session_setup):
        """Test bulk_create_memberships accepts role_id=0 (falsy but present)."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = [{"id": 1}]
        members = [{"email": "a@b.com", "role_id": 0}]
        result = src_server.bulk_create_memberships(21, members, session_id=session_id)
        assert result["status"] == "invited"

    def test_bulk_link_user_stories_to_epic(self, session_setup):
        """Test bulk_link_user_stories_to_epic calls correct endpoint."""
        session_id, mock_client = session_setup
        mock_client.api.post.return_value = None
        result = src_server.bulk_link_user_stories_to_epic(21, 10, [1, 2, 3], session_id=session_id)
        mock_client.api.post.assert_called_once_with(
            "/epics/10/related_userstories/bulk_create",
            json={"project_id": 21, "bulk_userstories": [1, 2, 3]},
        )
        assert result["status"] == "linked"
        assert result["count"] == 3

    def test_bulk_link_user_stories_to_epic_empty_raises(self):
        """Test bulk_link_user_stories_to_epic raises on empty list before session."""
        with pytest.raises(ValueError, match="user_story_ids list cannot be empty"):
            src_server.bulk_link_user_stories_to_epic(21, 10, [])

    def test_bulk_create_user_stories_no_session_raises(self):
        """Test bulk_create_user_stories raises when no session available."""
        src_server.active_sessions.clear()
        with pytest.raises(ValueError, match="No session_id provided"):
            src_server.bulk_create_user_stories(21, ["Story A"])


# ─── Response Filtering tests ─────────────────────────────────────────


class TestResponseFiltering:
    """Tests for the response filtering functionality."""

    def test_filter_standard_always_includes_version(self):
        """version is required for updates in standard level.

        Exceptions: member (read-only, no update endpoint) and swimlane
        (Taiga API does not use optimistic concurrency on /swimlanes —
        empirically no version field on the resource, see issue #24).
        """
        no_version = {"member", "swimlane"}
        for resource_type, levels in src_server.RESPONSE_FIELDS.items():
            if resource_type not in no_version:
                assert "version" in levels["standard"], (
                    f"{resource_type} missing version in standard"
                )

    def test_filter_minimal_includes_id(self):
        """All minimal levels must include id."""
        for resource_type, levels in src_server.RESPONSE_FIELDS.items():
            assert "id" in levels["minimal"], f"{resource_type} missing id in minimal"

    def test_filter_minimal_includes_project_where_applicable(self):
        """Resources with project association must include project in minimal."""
        project_resources = [
            "user_story",
            "task",
            "issue",
            "epic",
            "milestone",
            "wiki_page",
            "swimlane",
        ]
        for resource_type in project_resources:
            assert "project" in src_server.RESPONSE_FIELDS[resource_type]["minimal"], (
                f"{resource_type} missing project in minimal"
            )

    def test_filter_swimlane_verbosity_levels(self):
        """Swimlane response filter trims correctly per verbosity (issue #24)."""
        full = {
            "id": 1,
            "name": "Security",
            "order": 100,
            "project": 9,
            "statuses": [{"id": 65, "name": "New"}],  # large nested data, only in 'full'
        }
        minimal = src_server._filter_response(full, "swimlane", "minimal")
        assert set(minimal.keys()) == {"id", "name", "project"}
        standard = src_server._filter_response(full, "swimlane", "standard")
        assert set(standard.keys()) == {"id", "name", "order", "project"}
        result_full = src_server._filter_response(full, "swimlane", "full")
        assert result_full == full  # untouched
        assert "statuses" in result_full

    def test_filter_response_handles_none(self):
        """_filter_response should return None when given None."""
        assert src_server._filter_response(None, "user_story") is None

    def test_filter_response_handles_empty_list(self):
        """_filter_response should return empty list when given empty list."""
        assert src_server._filter_response([], "user_story") == []

    def test_filter_response_unknown_type_returns_full(self):
        """Unknown resource types should return full response."""
        data = {"id": 1, "extra": "field"}
        assert src_server._filter_response(data, "unknown_type") == data

    def test_filter_response_full_verbosity_returns_all(self):
        """Full verbosity should return all fields."""
        data = {
            "id": 1,
            "subject": "Test",
            "version": 1,
            "watchers": [1, 2],
            "extra_field": "value",
        }
        result = src_server._filter_response(data, "user_story", verbosity="full")
        assert result == data

    def test_filter_response_standard_filters_fields(self):
        """Standard verbosity should filter to defined fields."""
        data = {
            "id": 1,
            "ref": 123,
            "subject": "Test",
            "description": "Desc",
            "status": 1,
            "version": 2,
            "watchers": [1, 2],
            "extra_internal_field": "should_be_filtered",
        }
        result = src_server._filter_response(data, "user_story", verbosity="standard")
        assert "id" in result
        assert "ref" in result
        assert "subject" in result
        assert "version" in result
        assert "watchers" not in result
        assert "extra_internal_field" not in result

    def test_filter_response_minimal_filters_to_core(self):
        """Minimal verbosity should filter to core identification fields."""
        data = {
            "id": 1,
            "ref": 123,
            "subject": "Test",
            "status": 1,
            "project": 10,
            "description": "Long description",
            "version": 2,
            "watchers": [1, 2],
        }
        result = src_server._filter_response(data, "user_story", verbosity="minimal")
        assert result == {"id": 1, "ref": 123, "subject": "Test", "status": 1, "project": 10}

    def test_filter_response_list_filters_each_item(self):
        """_filter_response should filter each item in a list."""
        data = [
            {"id": 1, "subject": "Story 1", "watchers": [1]},
            {"id": 2, "subject": "Story 2", "watchers": [2]},
        ]
        result = src_server._filter_response(data, "user_story", verbosity="minimal")
        assert len(result) == 2
        assert "watchers" not in result[0]
        assert "watchers" not in result[1]

    def test_filter_response_invalid_verbosity_falls_back_to_standard(self):
        """Typos in verbosity should warn and use standard."""
        data = {"id": 1, "subject": "Test", "version": 1, "watchers": [1, 2]}
        result = src_server._filter_response(data, "user_story", verbosity="stanard")  # typo
        assert "id" in result
        assert "version" in result
        assert "watchers" not in result


# ─── User Story Task Enrichment tests ────────────────────────────────


class TestEnrichUserStoryTasks:
    """Tests for the _enrich_user_story_tasks helper."""

    def _make_us_result(self, us_id=606, project_id=21):
        return {"id": us_id, "ref": 18, "subject": "Test US", "project": project_id, "tasks": []}

    def _make_mock_client(self, tasks=None):
        mock = MagicMock()
        mock.list_resources.return_value = tasks or []
        return mock

    def test_standard_verbosity_includes_tasks_at_minimal_level(self):
        """Standard verbosity should enrich with tasks filtered to minimal."""
        raw_tasks = [
            {
                "id": 100,
                "ref": 10,
                "subject": "Task 1",
                "status": 1,
                "project": 21,
                "description": "should be filtered",
                "version": 1,
            },
            {
                "id": 101,
                "ref": 11,
                "subject": "Task 2",
                "status": 2,
                "project": 21,
                "description": "should be filtered",
                "version": 1,
            },
        ]
        mock_client = self._make_mock_client(raw_tasks)
        us_result = self._make_us_result()

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "standard")

        assert len(result["tasks"]) == 2
        assert result["tasks"][0] == {
            "id": 100,
            "ref": 10,
            "subject": "Task 1",
            "status": 1,
            "project": 21,
        }
        # description should be filtered out at minimal level
        assert "description" not in result["tasks"][0]
        mock_client.list_resources.assert_called_once_with("tasks", project_id=21, user_story=606)

    def test_full_verbosity_includes_tasks_at_standard_level(self):
        """Full verbosity should enrich with tasks filtered to standard."""
        raw_tasks = [
            {
                "id": 100,
                "ref": 10,
                "subject": "Task 1",
                "status": 1,
                "status_extra_info": {"name": "New"},
                "assigned_to": 9,
                "assigned_to_extra_info": {"username": "user1"},
                "user_story": 606,
                "milestone": 1,
                "project": 21,
                "description": "A task",
                "tags": [],
                "is_blocked": False,
                "due_date": None,
                "version": 1,
                "watchers": [1],
            },
        ]
        mock_client = self._make_mock_client(raw_tasks)
        us_result = self._make_us_result()

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "full")

        assert len(result["tasks"]) == 1
        assert "description" in result["tasks"][0]
        assert "status_extra_info" in result["tasks"][0]
        assert "assigned_to" in result["tasks"][0]
        # watchers should be filtered out at standard level
        assert "watchers" not in result["tasks"][0]

    def test_minimal_verbosity_skips_enrichment(self):
        """Minimal verbosity should not fetch tasks at all."""
        mock_client = self._make_mock_client()
        us_result = self._make_us_result()

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "minimal")

        assert result["tasks"] == []
        mock_client.list_resources.assert_not_called()

    def test_graceful_fallback_on_fetch_failure(self):
        """Should return empty tasks list if fetch fails."""
        mock_client = MagicMock()
        mock_client.list_resources.side_effect = Exception("Connection refused")
        us_result = self._make_us_result()

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "standard")

        assert result["tasks"] == []

    def test_missing_id_skips_enrichment(self):
        """Should skip enrichment if US result has no id."""
        mock_client = self._make_mock_client()
        us_result = {"ref": 18, "subject": "Test", "project": 21}

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "standard")

        assert "tasks" not in result
        mock_client.list_resources.assert_not_called()

    def test_missing_project_skips_enrichment(self):
        """Should skip enrichment if US result has no project."""
        mock_client = self._make_mock_client()
        us_result = {"id": 606, "ref": 18, "subject": "Test"}

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "standard")

        assert "tasks" not in result
        mock_client.list_resources.assert_not_called()

    def test_mutates_input_dict(self):
        """Should mutate the input dict in-place (documented behavior)."""
        mock_client = self._make_mock_client([])
        us_result = self._make_us_result()

        result = src_server._enrich_user_story_tasks(us_result, mock_client, "standard")

        assert result is us_result


# ─── Config tests ─────────────────────────────────────────────────────


class TestConfig:
    """Tests for the configuration module."""

    def test_mask_credential_normal(self):
        """Test masking a normal-length credential."""
        from src.config import mask_credential

        result = mask_credential("mysecretpassword")
        assert result.startswith("my")
        assert result.endswith("rd")
        assert "****" in result

    def test_mask_credential_short(self):
        """Test masking a short credential."""
        from src.config import mask_credential

        result = mask_credential("ab")
        assert result == "**"

    def test_mask_credential_empty(self):
        """Test masking an empty credential."""
        from src.config import mask_credential

        assert mask_credential("") == "<empty>"

    def test_taiga_settings_defaults(self):
        """Test TaigaSettings default values."""
        from src.config import TaigaSettings

        # Create with explicit values to avoid env pollution
        s = TaigaSettings(TAIGA_API_URL="http://test:9000")
        assert s.host == "http://test:9000"

    def test_taiga_settings_has_credentials_false(self):
        """Test has_credentials when no credentials set."""
        from src.config import TaigaSettings

        s = TaigaSettings(TAIGA_API_URL="http://test:9000")
        # If env vars are not set, credentials should be None
        if s.username is None and s.password is None:
            assert s.has_credentials is False


# ─── TaigaClientWrapper tests ────────────────────────────────────────


class TestTaigaClientWrapper:
    """Tests for the TaigaClientWrapper class."""

    def test_init_requires_host(self):
        """Test wrapper requires a host."""
        with pytest.raises(ValueError, match="Taiga host URL cannot be empty"):
            TaigaClientWrapper(host="")

    def test_init_sets_host(self):
        """Test wrapper stores the host."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        assert wrapper.host == "http://test:9000"
        assert wrapper.api is None

    def test_is_authenticated_false_initially(self):
        """Test wrapper is not authenticated initially."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        assert wrapper.is_authenticated is False

    def test_ensure_authenticated_raises(self):
        """Test _ensure_authenticated raises when not authenticated."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        with pytest.raises(PermissionError, match="Client not authenticated"):
            wrapper._ensure_authenticated()

    def test_list_resources_requires_auth(self):
        """Test list_resources raises when not authenticated."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        with pytest.raises(PermissionError):
            wrapper.list_resources("projects")

    def test_list_resources_sends_disable_pagination_header(self):
        """Test list_resources sends x-disable-pagination header."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = [{"id": 1}]
        wrapper.list_resources("projects")
        wrapper.api.get.assert_called_once_with(
            "/projects", params={}, headers={"x-disable-pagination": "True"}
        )

    def test_list_resources_endpoint_mapping(self):
        """Test list_resources maps resource types to correct endpoints."""
        from src.taiga_client import _RESOURCE_ENDPOINTS

        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        for resource_type, endpoint in _RESOURCE_ENDPOINTS.items():
            wrapper.api.get.reset_mock()
            wrapper.list_resources(resource_type)
            call_args = wrapper.api.get.call_args
            assert call_args[0][0] == endpoint, f"{resource_type} -> {endpoint}"

    def test_list_resources_with_filters(self):
        """Test list_resources passes filters as params."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = [{"id": 1}]
        result = wrapper.list_resources("issues", project_id=123, status=2)
        wrapper.api.get.assert_called_once_with(
            "/issues",
            params={"project": 123, "status": 2},
            headers={"x-disable-pagination": "True"},
        )
        assert result == [{"id": 1}]

    def test_list_resources_user_stories_swimlane_dual_emit(self):
        """Test list_resources sends both 'swimlane' and 'swimnlane' for user_stories.

        Workaround for upstream taiga-back typo (#68): SwimlanesFilter declares
        param_name="swimnlane" so ?swimlane= is silently ignored by the backend.
        """
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, swimlane=17)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["swimlane"] == 17
        assert call_params["swimnlane"] == 17
        assert call_params["project"] == 9

    def test_list_resources_user_stories_no_swimlane_unchanged(self):
        """Test list_resources does not inject 'swimnlane' when no 'swimlane' filter."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, status=65)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert "swimnlane" not in call_params
        assert "swimlane" not in call_params

    def test_list_resources_user_stories_swimnlane_explicit_preserved(self):
        """Test caller-supplied 'swimnlane' is not overwritten by translation."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, swimlane=17, swimnlane=99)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["swimlane"] == 17
        assert call_params["swimnlane"] == 99

    def test_list_resources_other_resource_swimlane_not_translated(self):
        """Test 'swimlane' filter on non-user_stories endpoints is not translated."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("tasks", project_id=9, swimlane=17)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["swimlane"] == 17
        assert "swimnlane" not in call_params

    def test_list_resources_user_stories_exclude_swimlane_not_translated(self):
        """Test 'exclude_swimlane' is passed through unchanged (no upstream typo there)."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, exclude_swimlane=17)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["exclude_swimlane"] == 17
        assert "exclude_swimnlane" not in call_params
        assert "swimlane" not in call_params
        assert "swimnlane" not in call_params

    def test_list_resources_no_project_id(self):
        """Test list_resources omits project key when project_id is None."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = [{"id": 1}]
        wrapper.list_resources("projects")
        call_params = wrapper.api.get.call_args[1]["params"]
        assert "project" not in call_params

    def test_list_resources_unknown_type(self):
        """Test list_resources raises for unknown resource type."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "test-token"
        with pytest.raises(ValueError, match="Unknown resource type"):
            wrapper.list_resources("nonexistent")

    # ─── _parse_mcp_kwargs JSON error handling tests (PR: fix/kwargs-json-parsing) ──

    def test_parse_mcp_kwargs_valid_json(self):
        """Test that valid JSON in kwargs is parsed correctly."""
        result = src_server._parse_mcp_kwargs({"kwargs": '{"key": "value"}'})
        assert result == {"key": "value"}

    def test_parse_mcp_kwargs_invalid_json_raises_valueerror(self):
        """Test that invalid JSON raises ValueError with descriptive message."""
        with pytest.raises(ValueError, match="Invalid JSON in 'kwargs' parameter"):
            src_server._parse_mcp_kwargs({"kwargs": "{1: 3}"})

    def test_parse_mcp_kwargs_filters_invalid_json_raises_valueerror(self):
        """Test that invalid JSON in 'filters' key raises ValueError with correct key name."""
        with pytest.raises(ValueError, match="Invalid JSON in 'filters' parameter"):
            src_server._parse_mcp_kwargs({"filters": "{bad}"})

    def test_parse_mcp_kwargs_empty_string_returns_empty_dict(self):
        """Test that empty string returns empty dict."""
        result = src_server._parse_mcp_kwargs({"kwargs": ""})
        assert result == {}
