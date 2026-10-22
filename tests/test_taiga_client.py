"""Unit tests for taiga_client.py: the native HTTP client, errors, wrapper and resolvers."""

import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.taiga_client import (
    TaigaAPIError,
    TaigaAuthenticationError,
    TaigaBadRequestError,
    TaigaClientWrapper,
    TaigaException,
    TaigaHTTP,
    TaigaNotFoundError,
    TaigaServerError,
    _error_detail,
    resolve_user_id,
    safe_lower,
)

HOST = "https://taiga.example"


def _http(handler):
    """A TaigaHTTP whose requests are answered by `handler(request) -> httpx.Response`."""
    return TaigaHTTP(HOST, transport=httpx.MockTransport(handler))


def _authed(handler, token="t1", refresh="r1"):
    api = _http(handler)
    api.auth_token, api._refresh_token = token, refresh
    return api


class TestTaigaHTTPRequests:
    def test_builds_api_v1_url_and_bearer_header(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": 1})

        assert _authed(handler).get("/userstories/by_ref", params={"ref": 7}) == {"id": 1}
        assert seen["url"] == f"{HOST}/api/v1/userstories/by_ref?ref=7"
        assert seen["auth"] == "Bearer t1"

    def test_json_body_and_custom_headers_sent(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["pagination"] = request.headers.get("x-disable-pagination")
            return httpx.Response(201, json={"ok": True})

        api = _authed(handler)
        api.post("/tasks", json={"subject": "s"}, headers={"x-disable-pagination": "True"})
        assert seen == {"body": {"subject": "s"}, "pagination": "True"}

    def test_params_encoded_like_requests(self):
        # requests (pytaigaclient) dropped None and sent booleans as "True"/"False".
        seen = {}

        def handler(request):
            seen["query"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        _authed(handler).get("/issues", params={"project": 1, "is_closed": False, "tag": None})
        assert seen["query"] == {"project": "1", "is_closed": "False"}

    def test_empty_body_returns_none(self):
        api = _authed(lambda request: httpx.Response(204))
        assert api.delete("/userstories/5") is None

    def test_non_json_success_returns_text(self):
        api = _authed(lambda request: httpx.Response(200, text="plain"))
        assert api.get("/x") == "plain"

    def test_follows_redirects(self):
        def handler(request):
            if request.url.path == "/api/v1/old":
                return httpx.Response(301, headers={"location": f"{HOST}/api/v1/new"})
            return httpx.Response(200, json={"moved": True})

        assert _authed(handler).get("/old") == {"moved": True}

    def test_transport_error_becomes_taiga_exception(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(TaigaException, match="Request failed: ConnectError"):
            _authed(handler).get("/projects")


class TestTaigaHTTPErrors:
    @pytest.mark.parametrize(
        "code,cls",
        [
            (400, TaigaBadRequestError),
            (401, TaigaAuthenticationError),
            (403, TaigaAuthenticationError),
            (404, TaigaNotFoundError),
            (500, TaigaServerError),
        ],
    )
    def test_status_maps_to_error_class(self, code, cls):
        api = _http(lambda request: httpx.Response(code, json={"_error_message": "nope"}))
        with pytest.raises(cls) as ei:
            api.get("/x")
        assert ei.value.status_code == code
        assert isinstance(ei.value, TaigaAPIError)

    def test_field_errors_are_kept(self):
        # The real 400 body shape from taiga.tetra-ai.fr (POST /userstories without
        # a subject). pytaigaclient reported "No error message provided by API.".
        api = _http(
            lambda request: httpx.Response(400, json={"subject": ["This field is required."]})
        )
        with pytest.raises(TaigaBadRequestError) as ei:
            api.post("/userstories", json={"project": 13})
        assert ei.value.error_detail == "subject: This field is required."
        assert str(ei.value) == "API Error 400: subject: This field is required."
        assert ei.value.body == {"subject": ["This field is required."]}

    def test_login_failure_detail_kept(self):
        api = _http(
            lambda request: httpx.Response(
                401,
                json={
                    "detail": "No active account found with the given credentials",
                    "code": "invalid_credentials",
                },
            )
        )
        with pytest.raises(TaigaAuthenticationError, match="No active account found"):
            api.login("u", "p")


class TestErrorDetail:
    """The DRF-body formatting formerly done by the pre-v3 _repair_taiga_api_error."""

    def test_error_message_wins(self):
        assert _error_detail({"_error_message": "boom", "x": ["y"]}, "", 400) == "boom"

    def test_detail_string(self):
        assert _error_detail({"detail": "bad", "meta": {"k": "v"}}, "", 422) == "bad"

    def test_field_errors_joined(self):
        detail = _error_detail(
            {"user_story": ["This field is required."], "epic": ["This field is required."]},
            "",
            400,
        )
        assert "user_story: This field is required." in detail
        assert "epic: This field is required." in detail

    def test_scalar_and_nested_values(self):
        detail = _error_detail({"count": 3, "meta": {"k": "v"}}, "", 400)
        assert "count: 3" in detail
        assert 'meta: {"k": "v"}' in detail

    def test_list_body(self):
        assert _error_detail(["a", "b"], "", 400) == "a; b"

    def test_non_json_falls_back_to_text_then_status(self):
        assert _error_detail(None, "Bad Gateway", 502) == "Bad Gateway"
        assert _error_detail(None, "", 502) == "HTTP 502 with an empty body"
        assert _error_detail({}, "", 400) == "HTTP 400 with an empty body"


class TestTaigaHTTPAuth:
    def test_login_stores_tokens_and_sends_no_bearer(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"auth_token": "a1", "refresh": "r1", "id": 6})

        api = _http(handler)
        api.login("rva", "pw")
        assert api.auth_token == "a1" and api._refresh_token == "r1"
        assert seen == {
            "auth": None,
            "body": {"type": "normal", "username": "rva", "password": "pw"},
        }

    def test_login_without_token_raises(self):
        api = _http(lambda request: httpx.Response(200, json={"id": 6}))
        with pytest.raises(TaigaException, match="no auth_token"):
            api.login("u", "p")

    def test_401_refreshes_once_and_retries(self):
        calls = []

        def handler(request):
            calls.append((request.url.path, request.headers.get("authorization")))
            if request.url.path == "/api/v1/auth/refresh":
                assert json.loads(request.content) == {"refresh": "r1"}
                return httpx.Response(200, json={"auth_token": "t2", "refresh": "r2"})
            if request.headers.get("authorization") == "Bearer t1":
                return httpx.Response(401, json={"detail": "Token is invalid or expired"})
            return httpx.Response(200, json={"ok": True})

        api = _authed(handler)
        assert api.get("/users/me") == {"ok": True}
        assert calls == [
            ("/api/v1/users/me", "Bearer t1"),
            ("/api/v1/auth/refresh", None),
            ("/api/v1/users/me", "Bearer t2"),
        ]
        # Taiga rotates refresh tokens: the new one must replace the spent one.
        assert (api.auth_token, api._refresh_token) == ("t2", "r2")

    def test_failed_refresh_surfaces_original_401(self):
        def handler(request):
            if request.url.path == "/api/v1/auth/refresh":
                return httpx.Response(401, json={"detail": "Token is invalid or expired"})
            return httpx.Response(401, json={"detail": "Given token not valid"})

        with pytest.raises(TaigaAuthenticationError, match="Given token not valid"):
            _authed(handler).get("/users/me")

    def test_rejected_refresh_token_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/v1/auth/refresh":
                return httpx.Response(401, json={"detail": "Token is invalid or expired"})
            return httpx.Response(401, json={"detail": "Given token not valid"})

        api = _authed(handler)
        for _ in range(2):
            with pytest.raises(TaigaAuthenticationError):
                api.get("/users/me")
        # The dead refresh token is spent once, not once per call.
        assert calls == ["/api/v1/users/me", "/api/v1/auth/refresh", "/api/v1/users/me"]
        assert api._refresh_token is None

    def test_no_refresh_token_means_no_retry(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401, json={"detail": "expired"})

        api = _authed(handler, refresh=None)
        with pytest.raises(TaigaAuthenticationError):
            api.get("/users/me")
        assert calls == ["/api/v1/users/me"]

    def test_concurrent_401s_spend_the_refresh_token_once(self):
        """Refresh tokens rotate (reusing one is a 401 on taiga.tetra-ai.fr), so two
        threads hitting 401 together must not both refresh, or the session dies."""
        refreshes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def handler(request):
            if request.url.path == "/api/v1/auth/refresh":
                with lock:
                    refreshes.append(json.loads(request.content)["refresh"])
                    spent = len(refreshes) > 1
                time.sleep(0.05)  # widen the race window
                if spent:
                    return httpx.Response(401, json={"detail": "Token is invalid or expired"})
                return httpx.Response(200, json={"auth_token": "t2", "refresh": "r2"})
            if request.headers.get("authorization") == "Bearer t1":
                barrier.wait(timeout=5)  # both requests fail with the old token together
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(200, json={"ok": True})

        api = _authed(handler)
        results, errors = [], []

        def call():
            try:
                results.append(api.get("/users/me"))
            except Exception as e:  # pragma: no cover - surfaced by the assert below
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        assert results == [{"ok": True}, {"ok": True}]
        assert refreshes == ["r1"]

    def test_never_logs_bodies_or_credentials(self, caplog):
        def handler(request):
            if request.url.path == "/api/v1/auth":
                return httpx.Response(200, json={"auth_token": "secret-token", "refresh": "r"})
            return httpx.Response(400, json={"subject": ["This field is required."]})

        api = _http(handler)
        with caplog.at_level(logging.DEBUG):
            api.login("rva", "hunter2")
            with pytest.raises(TaigaBadRequestError):
                api.post("/userstories", json={"description": "private text"})
        logged = caplog.text
        for secret in ("hunter2", "secret-token", "private text", "Bearer"):
            assert secret not in logged
        assert "POST userstories -> 400" in logged or "POST /userstories -> 400" in logged


class TestTaigaClientWrapperLogin:
    """Cover the auth path: empty-host guard, success, and the two failure modes."""

    def test_init_rejects_empty_host(self):
        with pytest.raises(ValueError, match="host URL cannot be empty"):
            TaigaClientWrapper(host="")

    def test_login_success_sets_api(self):
        wrapper = TaigaClientWrapper(host=HOST)
        fake_api = MagicMock()
        with patch("src.taiga_client.TaigaHTTP", return_value=fake_api):
            ok = wrapper.login(username="u", password="p")
        assert ok is True
        assert wrapper.api is fake_api
        fake_api.login.assert_called_once_with(username="u", password="p")

    def test_login_taiga_exception_propagates_and_clears_api(self):
        wrapper = TaigaClientWrapper(host=HOST)
        fake_api = MagicMock()
        fake_api.login.side_effect = TaigaException("bad creds")
        with patch("src.taiga_client.TaigaHTTP", return_value=fake_api):
            with pytest.raises(TaigaException, match="bad creds"):
                wrapper.login(username="u", password="p")
        assert wrapper.api is None

    def test_login_unexpected_error_wrapped(self):
        wrapper = TaigaClientWrapper(host=HOST)
        fake_api = MagicMock()
        fake_api.login.side_effect = RuntimeError("boom")
        with patch("src.taiga_client.TaigaHTTP", return_value=fake_api):
            with pytest.raises(TaigaException, match="Unexpected login error"):
                wrapper.login(username="u", password="p")
        assert wrapper.api is None

    def test_is_authenticated_tracks_token(self):
        wrapper = TaigaClientWrapper(host=HOST)
        assert wrapper.is_authenticated is False
        wrapper.api = _authed(lambda request: httpx.Response(200, json={}))
        assert wrapper.is_authenticated is True


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
        """A REAL /memberships payload.

        Membership rows carry no ``user_extra_info`` and no ``username`` field at
        all, and the flat ``email`` is frequently blank on established members
        while ``user_email`` holds the real address. The previous fixture supplied
        ``user_extra_info``, so username/display-name tests passed against a
        payload Taiga never sends while those code paths were dead in production.
        """
        return [
            # pending-invite membership with null fields — must not abort matching
            {"user": None, "email": None, "user_email": None, "full_name": None},
            {
                "user": 42,
                "email": "",
                "user_email": "alice@example.com",
                "full_name": "Alice Martin",
            },
        ]

    def _members_with_extra_info(self):
        """A non-memberships payload that DOES carry ``user_extra_info``.

        Only for the forward-compatibility lookups: ``resolve_user_id`` accepts
        member lists from other endpoints, so it still probes
        ``user_extra_info.username`` / ``.full_name_display``. Kept separate so it
        is never mistaken for a memberships shape.
        """
        return [
            {
                "user": 7,
                "user_extra_info": {"username": "carol", "full_name_display": "Carol Danvers"},
            }
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

    def test_resolves_by_user_email_when_flat_email_is_blank(self):
        # THE regression this fix exists for: on a real memberships row `email` is
        # "" and the address lives in `user_email`. Matching only the flat `email`
        # made assignment-by-email fail for every established member.
        assert resolve_user_id(self._members(), "alice@example.com") == 42

    def test_resolves_by_user_email_case_insensitive(self):
        assert resolve_user_id(self._members(), "ALICE@EXAMPLE.COM") == 42

    def test_resolves_by_flat_email_when_populated(self):
        # Some payloads do fill `email`; that path must keep working.
        members = [{"user": 9, "email": "dave@example.com", "user_email": None}]
        assert resolve_user_id(members, "dave@example.com") == 9

    def test_resolves_by_full_name(self):
        assert resolve_user_id(self._members(), "Alice Martin") == 42

    def test_resolves_by_full_name_case_insensitive(self):
        assert resolve_user_id(self._members(), "alice martin") == 42

    def test_resolves_by_username_on_non_memberships_payload(self):
        # Forward-compat only — memberships never carry username. See
        # _members_with_extra_info.
        assert resolve_user_id(self._members_with_extra_info(), "CAROL") == 7

    def test_resolves_by_display_name_on_non_memberships_payload(self):
        # Forward-compat only — see above.
        assert resolve_user_id(self._members_with_extra_info(), "carol danvers") == 7

    def test_null_field_member_does_not_crash(self):
        # The regression: the leading null-field member previously raised
        # "'NoneType' object has no attribute 'lower'" before reaching Alice.
        assert resolve_user_id(self._members(), "alice@example.com") == 42

    def test_skips_match_when_member_user_is_none(self):
        # A member matching by email but with user=None must not return None;
        # resolution continues / raises not-found rather than yielding a null ID.
        members = [{"user": None, "email": "ghost@example.com", "full_name": None}]
        with pytest.raises(ValueError, match="not found"):
            resolve_user_id(members, "ghost@example.com")

    def test_raises_when_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_user_id(self._members(), "bob")

    def test_not_found_error_falls_back_to_user_email_in_listing(self):
        # A member whose only address is user_email must still be named in the
        # diagnostic — it previously rendered "?" because only flat `email` was
        # consulted, and that is blank on real rows.
        with pytest.raises(ValueError, match="alice@example.com"):
            resolve_user_id([{"user": 42, "email": "", "user_email": "alice@example.com"}], "bob")

    def test_not_found_error_prefers_full_name_in_listing(self):
        with pytest.raises(ValueError, match="Alice Martin"):
            resolve_user_id(self._members(), "bob")


class TestGetResource:
    """get_resource fetches a single entity by ID (used for name-based assignment)."""

    def test_fetches_single_resource_by_id(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "tok"
        wrapper.api.get.return_value = {"id": 5, "project": 1}
        result = wrapper.get_resource("user_stories", 5)
        assert result == {"id": 5, "project": 1}
        wrapper.api.get.assert_called_once_with("/userstories/5")

    def test_rejects_unknown_resource_type(self):
        wrapper = TaigaClientWrapper(host="https://taiga.example")
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "tok"
        with pytest.raises(ValueError, match="Unknown resource type"):
            wrapper.get_resource("bogus", 5)


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
        wrapper.api = MagicMock(closed=False)
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
        wrapper.api = MagicMock(closed=False)
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
        wrapper.api = MagicMock(closed=False)
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
        wrapper.api = MagicMock(closed=False)
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
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, status=65)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert "swimnlane" not in call_params
        assert "swimlane" not in call_params

    def test_list_resources_user_stories_swimnlane_explicit_preserved(self):
        """Test caller-supplied 'swimnlane' is not overwritten by translation."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("user_stories", project_id=9, swimlane=17, swimnlane=99)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["swimlane"] == 17
        assert call_params["swimnlane"] == 99

    def test_list_resources_other_resource_swimlane_not_translated(self):
        """Test 'swimlane' filter on non-user_stories endpoints is not translated."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = []
        wrapper.list_resources("tasks", project_id=9, swimlane=17)
        call_params = wrapper.api.get.call_args[1]["params"]
        assert call_params["swimlane"] == 17
        assert "swimnlane" not in call_params

    def test_list_resources_user_stories_exclude_swimlane_not_translated(self):
        """Test 'exclude_swimlane' is passed through unchanged (no upstream typo there)."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock(closed=False)
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
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "test-token"
        wrapper.api.get.return_value = [{"id": 1}]
        wrapper.list_resources("projects")
        call_params = wrapper.api.get.call_args[1]["params"]
        assert "project" not in call_params

    def test_list_resources_unknown_type(self):
        """Test list_resources raises for unknown resource type."""
        wrapper = TaigaClientWrapper(host="http://test:9000")
        wrapper.api = MagicMock(closed=False)
        wrapper.api.auth_token = "test-token"
        with pytest.raises(ValueError, match="Unknown resource type"):
            wrapper.list_resources("nonexistent")


class TestCloseWhileInFlight:
    """Tools run in worker threads: a logout may close a client another thread is using."""

    def test_close_waits_for_in_flight_request_then_refuses_new_ones(self):
        entered, release = threading.Event(), threading.Event()

        def handler(request):
            entered.set()
            assert release.wait(timeout=5)
            return httpx.Response(200, json={"ok": True})

        api = _authed(handler)
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("r", api.get("/users/me")))
        t.start()
        assert entered.wait(timeout=5)

        api.close()  # e.g. logout from another thread, mid-request
        assert api.closed
        assert not api._http.is_closed, "pool closed under an in-flight request"

        release.set()
        t.join(timeout=5)
        assert result["r"] == {"ok": True}  # the in-flight request completed
        assert api._http.is_closed  # ...and the pool closed after it
        with pytest.raises(TaigaException, match="session closed"):
            api.get("/users/me")

    def test_idle_close_is_immediate(self):
        api = _authed(lambda request: httpx.Response(200, json={}))
        api.close()
        assert api._http.is_closed

    def test_wrapper_close_keeps_api_but_unauthenticates(self):
        # A tool already running holds the wrapper; api must not become None under it.
        wrapper = TaigaClientWrapper(host=HOST)
        wrapper.api = _authed(lambda request: httpx.Response(200, json={}))
        wrapper.close()
        assert wrapper.api is not None
        assert wrapper.is_authenticated is False


class TestMultipartUpload:
    def test_data_and_files_sent_as_multipart(self):
        seen = {}

        def handler(request):
            seen["ctype"] = request.headers["content-type"]
            seen["body"] = request.content
            return httpx.Response(201, json={"id": 1})

        api = _authed(handler)
        api.post(
            "/userstories/attachments",
            data={"project": "13", "object_id": "50"},
            files={"attached_file": ("a.txt", b"hello", "text/plain")},
        )
        assert seen["ctype"].startswith("multipart/form-data")
        body = seen["body"]
        assert b'name="project"' in body and b"13" in body
        assert b'name="object_id"' in body
        assert b'filename="a.txt"' in body and b"hello" in body


def test_httpx_request_lines_not_logged_at_info():
    import src.server.core  # noqa: F401  (applies the server's logging setup)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
