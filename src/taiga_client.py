# taiga_client.py
import logging
from typing import Any, Dict, List, Optional

from pytaigaclient import TaigaClient
from pytaigaclient.exceptions import TaigaException

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


class _CompatTaigaClient(TaigaClient):
    """TaigaClient with a compatibility shim for pytaigaclient's `query_params` bug.

    Four Tasks methods — `list`, `get_by_ref`, `filters_data`, `list_attachments` —
    call ``self.client.get(endpoint, query_params=...)``, but ``TaigaClient.get``
    only accepts ``params=`` and forwards unknown kwargs into ``_request()``, which
    raises ``unexpected keyword argument 'query_params'``. Every other resource
    correctly uses ``params=``, so the defect is isolated to the Tasks resource
    (see issue #87, regression of #79). We can't patch the dependency — it's pinned
    to an upstream commit of talhaorak/pyTaigaClient — so we remap the stray kwarg
    here, at the one chokepoint every resource routes through. This is a no-op for
    correctly-written callers (no ``query_params`` kwarg → nothing to remap).
    """

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        if "query_params" in kwargs:
            query_params = kwargs.pop("query_params")
            if params is None:
                params = query_params
            elif query_params:
                # Explicit params win on key conflict; never reached from the
                # library (its buggy calls only pass query_params), but defensive.
                params = {**query_params, **params}
        return super().get(path, params=params, **kwargs)


class TaigaClientWrapper:
    """
    A wrapper around the pytaiga-client library to manage API instance
    and authentication state.
    """

    def __init__(self, host: str):
        if not host:
            raise ValueError("Taiga host URL cannot be empty.")
        # Store host, but initialize client later during login/token auth
        self.host = host
        # Use the new client type
        self.api: Optional[TaigaClient] = None
        logger.info(f"TaigaClientWrapper initialized for host: {self.host}")

    def login(self, username: str, password: str) -> bool:
        """
        Authenticates with the Taiga instance using username and password.
        Uses pytaigaclient.
        """
        try:
            # SECURITY: Don't log username to avoid credential exposure
            logger.info(f"Attempting login on {self.host}")
            # Initialize the client here (compat subclass; see _CompatTaigaClient).
            api_instance = _CompatTaigaClient(host=self.host)
            # Use the auth resource's login method
            api_instance.auth.login(username=username, password=password)
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
            # Wrap unexpected errors in TaigaException if needed, or re-raise
            raise TaigaException(f"Unexpected login error: {e}")

    # Add method for token authentication if needed by pytaigaclient
    # def set_token(self, token: str, token_type: str = "Bearer"):
    #     logger.info(f"Initializing TaigaClient with token on {self.host}")
    #     self.api = _CompatTaigaClient(host=self.host, auth_token=token, token_type=token_type)
    #     logger.info("TaigaClient initialized with token.")

    @property
    def is_authenticated(self) -> bool:
        """Checks if the client is currently authenticated (has an API instance with a token)."""
        # Check if api exists and has a token
        return self.api is not None and self.api.auth_token is not None

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
