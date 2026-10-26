# taiga_client.py
"""Native Taiga REST client: a thin httpx wrapper, no SDK.

Replaced pytaigaclient in v3. Tools call endpoints
directly (`api.get("/userstories/by_ref", params=...)`), so all this layer
does is the HTTP plumbing: auth with token refresh, Taiga error bodies kept
in the exception, and no logging of request bodies or headers.
"""

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Union

import httpx

logger = logging.getLogger(__name__)

# Endpoint mapping for all listable resources
_RESOURCE_ENDPOINTS = {
    "projects": "/projects",
    "user_stories": "/userstories",
    "tasks": "/tasks",
    "issues": "/issues",
    "epics": "/epics",
    "milestones": "/milestones",
    "wiki": "/wiki",
    "memberships": "/memberships",
    "userstory_statuses": "/userstory-statuses",
    "task_statuses": "/task-statuses",
    "issue_statuses": "/issue-statuses",
    "epic_statuses": "/epic-statuses",
    "issue_types": "/issue-types",
    "priorities": "/priorities",
    "severities": "/severities",
    "points": "/points",
    "swimlanes": "/swimlanes",
}

_NO_PAGINATION_HEADERS = {"x-disable-pagination": "True"}


def safe_lower(value: Any) -> str:
    """Lowercase a value, tolerating None.

    Taiga returns JSON ``null`` for unset string fields (e.g. a membership's
    ``email``/``full_name`` on a pending invitation, or a resource ``name``).
    A bare ``value.lower()`` on those raised ``'NoneType' object has no
    attribute 'lower'`` (pytaiga-mcp#120). Coercing ``None`` -> ``""`` first
    keeps name/username matching robust across both server flavors.
    """
    return (value or "").lower()


def resolve_user_id(members: List[Dict[str, Any]], identifier: "int | str") -> int:
    """Resolve a user identifier to a Taiga user ID against a project's memberships.

    ``identifier`` may be an ``int`` (returned unchanged — treated as a user ID)
    or a string matched case-insensitively against each member's ``user_email``,
    ``email``, ``full_name``, or — on non-memberships payloads only — the
    ``user_extra_info`` username / display name. Matching is None-safe (see
    ``safe_lower``): a single member with a null field no longer aborts resolution
    before the intended user is reached.

    Membership rows carry NO ``user_extra_info`` and no ``username`` field, so
    those lookups are kept only for forward-compatibility with endpoints that do
    return them — they never match on a memberships payload. ``user_email`` is
    the reliable address: the flat ``email`` field is frequently blank on
    established members, which is why email resolution needs both.

    Raises ``ValueError`` if the identifier is empty or not found.
    """
    if isinstance(identifier, bool):  # bool is an int subclass — reject to avoid surprises
        raise ValueError("User identifier must be an int user ID or a name/email string.")
    if isinstance(identifier, int):
        return identifier
    needle = safe_lower(identifier)
    if not needle:
        raise ValueError("Empty username/identifier for user resolution.")
    for m in members:
        info = m.get("user_extra_info") or {}
        if needle in (
            safe_lower(info.get("username")),
            safe_lower(m.get("user_email")),
            safe_lower(m.get("email")),
            safe_lower(m.get("full_name")),
            safe_lower(info.get("full_name_display")),
        ):
            uid = m.get("user")
            if uid is not None:
                return uid
    available = [
        m.get("full_name") or m.get("user_email") or m.get("email") or "?" for m in members
    ]
    raise ValueError(f"User '{identifier}' not found in project. Members: {available}")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaigaException(Exception):
    """Base class for Taiga client errors (transport failures included)."""


class TaigaAPIError(TaigaException):
    """Taiga answered with an error status. Keeps the decoded body.

    ``error_detail`` is always readable: Taiga's ``_error_message``, DRF's
    ``detail``, or the field errors of a 400 (``subject: This field is
    required.``). pytaigaclient read only ``_error_message`` and reported
    "No error message provided by API." for everything else.
    """

    def __init__(self, status_code: int, error_detail: str, body: Any = None):
        self.status_code = status_code
        self.error_detail = error_detail
        self.body = body
        super().__init__(f"API Error {status_code}: {error_detail}")


class TaigaAuthenticationError(TaigaAPIError):
    """401 or 403."""


class TaigaBadRequestError(TaigaAPIError):
    """Any other 4xx."""


class TaigaNotFoundError(TaigaAPIError):
    """404."""


class TaigaConcurrencyError(TaigaAPIError):
    """409, or Taiga's optimistic-concurrency 400 (body keyed on ``version``)."""


class TaigaRateLimitError(TaigaAPIError):
    """429."""


class TaigaServerError(TaigaAPIError):
    """5xx."""


def _error_detail(body: Any, text: str, status_code: int) -> str:
    """Turn a Taiga/DRF error body into one readable line."""
    if isinstance(body, dict) and body:
        for key in ("_error_message", "detail"):
            if isinstance(body.get(key), str) and body[key]:
                return body[key]
        parts = []
        for k, v in body.items():
            if isinstance(v, list):
                parts.append(f"{k}: {'; '.join(map(str, v))}")
            elif isinstance(v, (str, int, float, bool)) or v is None:
                parts.append(f"{k}: {v}")
            else:
                parts.append(f"{k}: {json.dumps(v)}")
        return " | ".join(parts)
    if isinstance(body, list) and body:
        return "; ".join(map(str, body))
    return text.strip()[:500] or f"HTTP {status_code} with an empty body"


def _api_error(response: httpx.Response) -> TaigaAPIError:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = response.status_code
    detail = _error_detail(body, response.text, code)
    if code in (401, 403):
        cls = TaigaAuthenticationError
    elif code == 404:
        cls = TaigaNotFoundError
    elif code == 409 or (code == 400 and isinstance(body, dict) and "version" in body):
        # Taiga refuses a stale `version` with 400 {"version": ...}, not 409.
        cls = TaigaConcurrencyError
    elif code == 429:
        cls = TaigaRateLimitError
    elif 400 <= code < 500:
        cls = TaigaBadRequestError
    elif code >= 500:
        cls = TaigaServerError
    else:
        cls = TaigaAPIError
    return cls(code, detail, body)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _encode_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Encode query params the way requests (and so pytaigaclient) did.

    requests drops None values and sends booleans as "True"/"False"; httpx
    would send None as an empty value and booleans as "true"/"false". Keeping
    the old encoding means tools ported from the pre-v3 server send exactly
    the query strings they were tested with.
    """
    if not params:
        return params
    encoded: Dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        encoded[k] = str(v) if isinstance(v, bool) else v
    return encoded


class TaigaHTTP:
    """Authenticated HTTP access to one Taiga instance's /api/v1.

    ``get/post/patch/put/delete(path, params=, json=, files=, headers=)``
    return the decoded JSON body, ``None`` for an empty body (204), or the
    raw text for a non-JSON success. Errors raise ``TaigaAPIError``
    subclasses; transport failures raise ``TaigaException``.

    Access tokens live 24 h and refresh tokens 8 days on a default Taiga,
    and refresh tokens ROTATE: once used, the old one is rejected. On a 401
    the request is retried once after a refresh. The refresh runs under a
    lock, and a caller that waited re-checks whether another thread already
    refreshed, so concurrent 401s never spend the same refresh token twice
    (which would lock the session out).
    """

    API_PATH = "/api/v1/"

    def __init__(
        self,
        host: str,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not host:
            raise ValueError("Taiga host URL cannot be empty.")
        # TAIGA_API_URL is the host; API_PATH is added below. Accept a URL that
        # already ends in /api/v1 too (many configs do), instead of 404ing on
        # .../api/v1/api/v1/auth (#197).
        self.host = host.rstrip("/").removesuffix(self.API_PATH.rstrip("/")).rstrip("/")
        self.auth_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._refresh_lock = threading.Lock()
        # Tools run in worker threads (src/server/app.py:tool), so close() can be
        # called while another thread's request is in flight. Close is deferred
        # until the last in-flight request returns; new requests are refused.
        self._state_lock = threading.Lock()
        self._inflight = 0
        self._closing = False
        self._http = httpx.Client(
            base_url=self.host + self.API_PATH,
            timeout=timeout,
            # httpx doesn't follow redirects by default (requests did). Reads
            # survive an http->https or trailing-slash redirect, but a 301/302
            # turns POST into a body-less GET: configure TAIGA_API_URL with
            # the final https URL.
            follow_redirects=True,
            headers={"Accept": "application/json", "Accept-Language": "en"},
            transport=transport,
        )

    # -- auth ---------------------------------------------------------------

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """POST /auth (normal login). Stores the access and refresh tokens."""
        data = self._send(
            "POST",
            "auth",
            json={"type": "normal", "username": username, "password": password},
            authenticate=False,
        )
        if not isinstance(data, dict) or not data.get("auth_token"):
            raise TaigaException("Login response carried no auth_token.")
        self.auth_token = data["auth_token"]
        self._refresh_token = data.get("refresh")
        return data

    def refresh(self, stale_token: Optional[str]) -> bool:
        """Refresh the access token once; True if a fresh token is in place.

        ``stale_token`` is the token the caller's request was rejected with.
        If another thread already replaced it, there is nothing to do.
        """
        with self._refresh_lock:
            if self.auth_token != stale_token:
                return self.auth_token is not None
            if not self._refresh_token:
                return False
            try:
                data = self._send(
                    "POST",
                    "auth/refresh",
                    json={"refresh": self._refresh_token},
                    authenticate=False,
                )
            except (TaigaAuthenticationError, TaigaBadRequestError) as e:
                # Taiga rejected the refresh token (expired or already rotated):
                # it will never work again, so stop re-trying it on every call.
                # A 5xx/429/transport failure falls through and keeps it.
                logger.warning(f"Taiga token refresh rejected: {e}")
                self._refresh_token = None
                return False
            except TaigaException as e:
                logger.warning(f"Taiga token refresh failed: {e}")
                return False
            if not isinstance(data, dict) or not data.get("auth_token"):
                return False
            self.auth_token = data["auth_token"]
            self._refresh_token = data.get("refresh") or self._refresh_token
            logger.info("Taiga access token refreshed.")
            return True

    # -- requests -----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        files: Any = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], List[Any], str, None]:
        return self._send(
            method, path, params=params, json=json, files=files, headers=headers, data=data
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        return self.request("GET", path, params=params, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json=json, **kwargs)

    def patch(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, json=json, **kwargs)

    def delete(self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        return self.request("DELETE", path, params=params, **kwargs)

    def close(self) -> None:
        """Close the connection pool, once no request is in flight."""
        with self._state_lock:
            self._closing = True
            idle = self._inflight == 0
        if idle:
            self._http.close()

    @property
    def closed(self) -> bool:
        return self._closing

    def _send(self, *args: Any, **kwargs: Any) -> Union[Dict[str, Any], List[Any], str, None]:
        with self._state_lock:
            if self._closing:
                raise TaigaException("Taiga session closed (logged out); log in again.")
            self._inflight += 1
        try:
            return self._send_open(*args, **kwargs)
        finally:
            with self._state_lock:
                self._inflight -= 1
                close_now = self._closing and self._inflight == 0
            if close_now:
                self._http.close()

    def _send_open(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        files: Any = None,
        headers: Optional[Dict[str, str]] = None,
        authenticate: bool = True,
        data: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], List[Any], str, None]:
        token = self.auth_token if authenticate else None
        response = self._raw(method, path, params, json, files, headers, token, data)
        if response.status_code == 401 and token is not None and self.refresh(token):
            response = self._raw(method, path, params, json, files, headers, self.auth_token, data)
        # Method, path and status only: never bodies or headers (credentials).
        logger.debug(f"{method} {path} -> {response.status_code}")
        if not response.is_success:
            raise _api_error(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _raw(self, method, path, params, json, files, headers, token, data=None) -> httpx.Response:
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        try:
            return self._http.request(
                method,
                path.lstrip("/"),
                params=_encode_params(params),
                json=json,
                # Form fields; with files= this is a multipart upload (attachments).
                data=data,
                files=files,
                headers=request_headers,
            )
        except httpx.HTTPError as e:
            raise TaigaException(f"Request failed: {type(e).__name__}: {e}") from e


class TaigaClientWrapper:
    """Holds one session's Taiga client (``api``) and its authentication state."""

    def __init__(self, host: str):
        if not host:
            raise ValueError("Taiga host URL cannot be empty.")
        # Store host, but initialize client later during login
        self.host = host
        self.api: Optional[TaigaHTTP] = None
        logger.info(f"TaigaClientWrapper initialized for host: {self.host}")

    def login(self, username: str, password: str) -> bool:
        """Authenticates with the Taiga instance using username and password."""
        try:
            # SECURITY: Don't log username to avoid credential exposure
            logger.info(f"Attempting login on {self.host}")
            api_instance = TaigaHTTP(host=self.host)
            try:
                api_instance.login(username=username, password=password)
            except BaseException:
                api_instance.close()
                raise
            self.api = api_instance
            logger.info("Login successful. Auth token acquired.")
            return True
        except TaigaException as e:
            # SECURITY: Don't log username in error messages
            logger.error(f"Taiga login failed: {e}", exc_info=False)
            self.api = None
            raise e
        except Exception as e:
            # SECURITY: Don't log username in error messages
            logger.error(f"An unexpected error occurred during login: {e}", exc_info=True)
            self.api = None
            raise TaigaException(f"Unexpected login error: {e}")

    def close(self) -> None:
        """Release the HTTP connection pool. The wrapper is unauthenticated after.

        `api` is kept, not set to None: a tool already running in another thread
        holds this wrapper and would crash on `api.get` (AttributeError on None).
        Its in-flight request finishes; its next one gets a clear TaigaException.
        """
        if self.api is not None:
            self.api.close()

    @property
    def is_authenticated(self) -> bool:
        """Checks if the client is currently authenticated (has an API instance with a token)."""
        # Check if api exists, is still open, and has a token
        return self.api is not None and not self.api.closed and self.api.auth_token is not None

    def _ensure_authenticated(self):
        """Internal helper to check authentication before API calls."""
        if not self.is_authenticated:
            logger.error("Action required authentication, but client is not logged in.")
            raise PermissionError("Client not authenticated. Please login first.")

    def list_resources(
        self, resource_type: str, project_id: Optional[int] = None, **filters
    ) -> List[Dict[str, Any]]:
        """
        Unified interface for listing resources via raw API with pagination disabled.

        Uses the x-disable-pagination header to bypass Taiga's default PAGE_SIZE=30
        limit, ensuring all results are returned in a single request.

        Args:
            resource_type: The type of resource (e.g., 'user_stories', 'tasks', 'issues')
            project_id: The project ID to filter by (required for most resources)
            **filters: Additional filters to apply

        Returns:
            List of resource dictionaries
        """
        self._ensure_authenticated()
        endpoint = _RESOURCE_ENDPOINTS.get(resource_type)
        if endpoint is None:
            raise ValueError(
                f"Unknown resource type: {resource_type}. Valid: {sorted(_RESOURCE_ENDPOINTS)}"
            )
        params = {}
        if project_id is not None:
            params["project"] = project_id
        params.update(filters)
        # Workaround for upstream taiga-back typo (TETRA-2023/pytaiga-mcp#68):
        # SwimlanesFilter declares param_name="swimnlane" (extra 'n'), so the
        # back-end ignores ?swimlane= and only honours ?swimnlane=. Sending
        # both keys keeps us correct before and after upstream fixes the typo,
        # since the dispatcher uses whichever it recognises and ignores the
        # other. Scoped to user_stories list — only that endpoint has the bug.
        # Upstream ref:
        # https://github.com/taigaio/taiga-back/blob/df14a4bdaee662962e343e3c4cd3fcd6a1339de7/taiga/projects/userstories/filters.py#L31-L34
        # exclude_swimlane is unaffected (no typo there) and intentionally not translated.
        if resource_type == "user_stories" and "swimlane" in params and "swimnlane" not in params:
            params["swimnlane"] = params["swimlane"]
        result = self.api.get(endpoint, params=params, headers=_NO_PAGINATION_HEADERS)
        return result if isinstance(result, list) else []
