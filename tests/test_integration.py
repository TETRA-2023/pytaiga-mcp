"""Integration tests against a real Taiga instance (run with `-m integration`).

Read tests run against any account. Write tests need a sandbox project, named by
slug in TAIGA_TEST_PROJECT, and are skipped without it: they create stories and
tasks, and delete them again in `finally` through the session's raw client (the
server has no delete tool yet). Never point TAIGA_TEST_PROJECT at a real project.

    TAIGA_API_URL=... TAIGA_USERNAME=... TAIGA_PASSWORD=... \\
    TAIGA_TEST_PROJECT=<sandbox-slug> ./run_integration_tests.sh
"""

import os
import uuid

import pytest

import src.server_workflow as wf

TEST_HOST = os.environ.get(
    "TAIGA_TEST_HOST", os.environ.get("TAIGA_API_URL", "http://localhost:9000")
)
TEST_USERNAME = os.environ.get("TAIGA_TEST_USERNAME", os.environ.get("TAIGA_USERNAME", "test"))
TEST_PASSWORD = os.environ.get("TAIGA_TEST_PASSWORD", os.environ.get("TAIGA_PASSWORD", "test"))
TEST_PROJECT = os.environ.get("TAIGA_TEST_PROJECT")

needs_sandbox = pytest.mark.skipif(
    not TEST_PROJECT, reason="set TAIGA_TEST_PROJECT to a sandbox project slug"
)


@pytest.fixture
def session_id():
    """A real session, logged out afterwards."""
    sid = wf.login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)["session_id"]
    yield sid
    wf.logout(sid)


def _delete(session_id, resource, item_id):
    """Best-effort cleanup through the raw client (no delete tool until v3 B5)."""
    try:
        wf.active_sessions[session_id].api.delete(f"/{resource}/{item_id}")
    except Exception as e:  # leave a visible trace, don't mask the test's own failure
        print(f"cleanup: could not delete {resource}/{item_id}: {e}")


@pytest.mark.integration
class TestReadOnly:
    def test_list_projects_and_overview(self, session_id):
        projects = wf.list_projects(session_id=session_id)
        assert projects, "No projects visible to this account"
        slug = TEST_PROJECT or projects[0]["slug"]

        overview = wf.get_project_overview(slug, session_id=session_id)
        assert overview["project"]["slug"] == slug

    def test_session_status(self, session_id):
        status = wf.session_status(session_id=session_id)
        assert status["status"] == "active"


@pytest.mark.integration
@needs_sandbox
class TestSandboxWrites:
    def test_story_task_and_comment_round_trip(self, session_id):
        """create → get by ref → update → task → comment, on real endpoints.

        Covers the by-ref paths (stories and tasks) that the native client
        migration (v3 B2) rewrites, plus a write with optimistic concurrency.
        """
        subject = f"[TEST - CAN DELETE] round trip {uuid.uuid4()}"
        story = wf.create_story(TEST_PROJECT, subject, session_id=session_id)
        task_id = None
        try:
            got = wf.get_story(TEST_PROJECT, story["ref"], session_id=session_id)
            assert got["subject"] == subject

            wf.update_story(TEST_PROJECT, story["ref"], description="edited", session_id=session_id)
            assert (
                wf.get_story(TEST_PROJECT, story["ref"], session_id=session_id)["description"]
                == "edited"
            )

            task = wf.create_task(TEST_PROJECT, story["ref"], "child task", session_id=session_id)
            task_id = task["id"]
            assert wf.get_task(TEST_PROJECT, task["ref"], session_id=session_id)["subject"] == (
                "child task"
            )

            result = wf.add_comment(
                TEST_PROJECT, story["ref"], "integration comment", session_id=session_id
            )
            assert result["status"] == "comment_added"
        finally:
            if task_id is not None:
                _delete(session_id, "tasks", task_id)
            _delete(session_id, "userstories", story["id"])
