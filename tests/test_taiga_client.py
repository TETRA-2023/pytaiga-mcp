"""Unit tests for taiga_client.py — the _CompatTaigaClient query_params shim (#87)."""

from unittest.mock import patch

from src.taiga_client import _CompatTaigaClient


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
