"""Unit tests for taiga_client.py — the _CompatTaigaClient query_params shim (#87)."""

from unittest.mock import MagicMock, patch

import pytest
from pytaigaclient.exceptions import TaigaException

from src.taiga_client import (
    TaigaClientWrapper,
    _CompatTaigaClient,
    resolve_user_id,
    safe_lower,
)


def _client():
    # Constructor does no network I/O — it only builds a session + resource objects.
    return _CompatTaigaClient(host="https://taiga.example")


class TestCompatTaigaClientGet:
    """Regression guard for #87 — pytaigaclient's Tasks methods call
    get(endpoint, query_params=...), which the base TaigaClient.get rejects. The
    shim remaps that stray kwarg onto params= for every resource at one chokepoint."""

    def test_remaps_query_params_to_params(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value={"ok": True}) as req:
            result = c.get("/tasks/by_ref", query_params={"ref": 7, "project": 1})
        assert result == {"ok": True}
        assert req.call_args.kwargs["params"] == {"ref": 7, "project": 1}
        assert "query_params" not in req.call_args.kwargs

    def test_params_passthrough_unaffected(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value=[]) as req:
            c.get("/userstories", params={"project": 1})
        assert req.call_args.kwargs["params"] == {"project": 1}
        assert "query_params" not in req.call_args.kwargs

    def test_no_params_at_all_passes_none(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value=None) as req:
            c.get("/tasks")  # mirrors tasks.list() with query_params=None
        assert req.call_args.kwargs["params"] is None

    def test_explicit_params_win_on_conflict(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value=None) as req:
            c.get("/x", params={"a": "explicit"}, query_params={"a": "stray", "b": 2})
        assert req.call_args.kwargs["params"] == {"a": "explicit", "b": 2}


class TestBuggyTasksMethodsNowWork:
    """Drive the actual pytaigaclient Tasks methods (the ones that pass
    query_params=) through the shim and confirm they no longer raise and route
    params correctly. These are the latent siblings of the get_by_ref bug."""

    def test_get_by_ref(self):
        c = _client()
        with patch.object(
            _CompatTaigaClient, "_request", return_value={"id": 200, "ref": 7}
        ) as req:
            result = c.tasks.get_by_ref(ref=7, project=1)
        assert result == {"id": 200, "ref": 7}
        assert req.call_args.args == ("GET", "/tasks/by_ref")
        assert req.call_args.kwargs["params"] == {"ref": 7, "project": 1}
        assert "query_params" not in req.call_args.kwargs

    def test_list(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value=[]) as req:
            c.tasks.list(query_params={"project": 1, "user_story": 50})
        assert req.call_args.kwargs["params"] == {"project": 1, "user_story": 50}
        assert "query_params" not in req.call_args.kwargs

    def test_filters_data(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value={}) as req:
            c.tasks.filters_data(project_id=1)
        assert "query_params" not in req.call_args.kwargs
        assert req.call_args.kwargs["params"] == {"project": 1}

    def test_list_attachments(self):
        c = _client()
        with patch.object(_CompatTaigaClient, "_request", return_value=[]) as req:
            c.tasks.list_attachments(project=1, object_id=200)
        assert "query_params" not in req.call_args.kwargs
        assert req.call_args.kwargs["params"] == {"project": 1, "object_id": 200}


class TestTaigaClientWrapperLogin:
    """Cover the auth path: empty-host guard, success, and the two failure modes."""

    def test_init_rejects_empty_host(self):
        with pytest.raises(ValueError, match="host URL cannot be empty"):
            TaigaClientWrapper(host="")

    def test_login_success_sets_api(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        fake_api = MagicMock()
        with patch("src.taiga_client._CompatTaigaClient", return_value=fake_api):
            ok = wrapper.login(username="u", password="p")
        assert ok is True
        assert wrapper.api is fake_api
        fake_api.auth.login.assert_called_once_with(username="u", password="p")

    def test_login_taiga_exception_propagates_and_clears_api(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        fake_api = MagicMock()
        fake_api.auth.login.side_effect = TaigaException("bad creds")
        with patch("src.taiga_client._CompatTaigaClient", return_value=fake_api):
            with pytest.raises(TaigaException, match="bad creds"):
                wrapper.login(username="u", password="p")
        assert wrapper.api is None

    def test_login_unexpected_error_wrapped(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        fake_api = MagicMock()
        fake_api.auth.login.side_effect = RuntimeError("boom")
        with patch("src.taiga_client._CompatTaigaClient", return_value=fake_api):
            with pytest.raises(TaigaException, match="Unexpected login error"):
                wrapper.login(username="u", password="p")
        assert wrapper.api is None


class TestSafeLower:
    """None-safe lowercasing (pytaiga-mcp#120)."""

    def test_none_becomes_empty_string(self):
        assert safe_lower(None) == ""

    def test_lowercases_string(self):
        assert safe_lower("Alice@Example.COM") == "alice@example.com"

    def test_empty_string_stays_empty(self):
        assert safe_lower("") == ""


class TestResolveUserId:
    """Shared None-safe user resolver used by both server flavors (pytaiga-mcp#120)."""

    def _members(self):
        return [
            # pending-invite membership with null fields — must not abort matching
            {"user": None, "email": None, "full_name": None, "user_extra_info": None},
            {
                "user": 42,
                "email": "alice@example.com",
                "full_name": "Alice Martin",
                "user_extra_info": {"username": "alice", "full_name_display": "Alice Martin"},
            },
        ]

    def test_int_passes_through_without_lookup(self):
        # No members needed — an int is a user ID and is returned unchanged.
        assert resolve_user_id([], 42) == 42

    def test_bool_is_rejected(self):
        with pytest.raises(ValueError, match="int user ID or a name/email"):
            resolve_user_id(self._members(), True)

    def test_empty_identifier_rejected(self):
        with pytest.raises(ValueError, match="Empty username"):
            resolve_user_id(self._members(), "")

    def test_resolves_by_username_case_insensitive(self):
        assert resolve_user_id(self._members(), "ALICE") == 42

    def test_resolves_by_email(self):
        assert resolve_user_id(self._members(), "alice@example.com") == 42

    def test_resolves_by_full_name(self):
        assert resolve_user_id(self._members(), "Alice Martin") == 42

    def test_resolves_by_display_name(self):
        assert resolve_user_id(self._members(), "alice martin") == 42

    def test_null_field_member_does_not_crash(self):
        # The regression: the leading null-field member previously raised
        # "'NoneType' object has no attribute 'lower'" before reaching Alice.
        assert resolve_user_id(self._members(), "alice") == 42

    def test_skips_match_when_member_user_is_none(self):
        # A member matching by email but with user=None must not return None;
        # resolution continues / raises not-found rather than yielding a null ID.
        members = [{"user": None, "email": "ghost@example.com", "full_name": None}]
        with pytest.raises(ValueError, match="not found"):
            resolve_user_id(members, "ghost@example.com")

    def test_raises_when_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_user_id(self._members(), "bob")


class TestGetResource:
    """get_resource fetches a single entity by ID (used for name-based assignment)."""

    def test_fetches_single_resource_by_id(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "tok"
        wrapper.api.get.return_value = {"id": 5, "project": 1}
        result = wrapper.get_resource("user_stories", 5)
        assert result == {"id": 5, "project": 1}
        wrapper.api.get.assert_called_once_with("/userstories/5")

    def test_rejects_unknown_resource_type(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        wrapper.api = MagicMock()
        wrapper.api.auth_token = "tok"
        with pytest.raises(ValueError, match="Unknown resource type"):
            wrapper.get_resource("bogus", 5)
