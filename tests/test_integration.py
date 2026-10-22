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
from src.taiga_client import TaigaBadRequestError, TaigaNotFoundError

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

    def test_expired_access_token_is_refreshed(self, session_id):
        """A rejected access token triggers one real POST /auth/refresh, then a retry.

        Simulates the 24 h access-token expiry by corrupting the token in place.
        The refresh token must also rotate: Taiga rejects a spent one.
        """
        api = wf.active_sessions[session_id].api
        old_refresh = api._refresh_token
        api.auth_token = "expired-or-garbage"
        me = api.get("/users/me")
        assert me["username"]
        assert api.auth_token != "expired-or-garbage"
        assert api._refresh_token and api._refresh_token != old_refresh


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

    def test_validation_error_keeps_drf_body(self, session_id):
        """A real 400 surfaces Taiga's field errors (pre-v3: "No error message")."""
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        with pytest.raises(TaigaBadRequestError) as ei:
            # No subject: rejected by validation, nothing is created.
            wf.active_sessions[session_id].api.post("/userstories", json={"project": project_id})
        assert "subject" in ei.value.error_detail
        assert "No error message provided" not in str(ei.value)

    def test_issue_sprint_epic_assign_wiki_round_trip(self, session_id):
        """The rest of the v3 B2 rewrite on real endpoints: issues, sprints,
        epics (incl. the #143 per-story link), assignment and the wiki.

        Everything created here is deleted in `finally`, and the project's
        story count is checked unchanged — #143 used to inject a junk story.
        """
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        api = wf.active_sessions[session_id].api
        tag = uuid.uuid4().hex[:8]
        stories_before = len(
            wf.active_sessions[session_id].list_resources("user_stories", project_id=project_id)
        )
        made = {"userstories": [], "issues": [], "epics": [], "milestones": [], "wiki": []}
        try:
            a = wf.create_story(TEST_PROJECT, f"[TEST - CAN DELETE] a {tag}", session_id=session_id)
            b = wf.create_story(TEST_PROJECT, f"[TEST - CAN DELETE] b {tag}", session_id=session_id)
            made["userstories"] += [a["id"], b["id"]]

            # Issue: create -> update -> get by ref.
            issue = wf.create_issue(
                TEST_PROJECT, f"[TEST - CAN DELETE] issue {tag}", session_id=session_id
            )
            made["issues"].append(issue["id"])
            wf.update_issue(TEST_PROJECT, issue["ref"], description="edited", session_id=session_id)
            got = wf.get_issue(TEST_PROJECT, issue["ref"], session_id=session_id)
            assert got["description"] == "edited"

            # Sprint: plan with story a, then close it by name.
            name = f"[TEST] sprint {tag}"
            planned = wf.plan_sprint(
                TEST_PROJECT,
                name,
                "2026-01-01",
                "2026-01-14",
                story_refs=[a["ref"]],
                session_id=session_id,
            )
            made["milestones"].append(planned["sprint"]["id"])
            assert planned["stories_assigned"] == 1
            closed = wf.close_sprint(TEST_PROJECT, name, session_id=session_id)
            assert closed["status"] == "closed"

            # Epic linking both stories: the live check for #143.
            epic = wf.create_epic(
                TEST_PROJECT,
                f"[TEST - CAN DELETE] epic {tag}",
                story_refs=[a["ref"], b["ref"]],
                session_id=session_id,
            )
            made["epics"].append(epic["id"])
            assert epic["stories_linked"] == 2
            overview = wf.get_epic_overview(TEST_PROJECT, epic["ref"], session_id=session_id)
            assert sorted(s["ref"] for s in overview["stories"]) == sorted([a["ref"], b["ref"]])

            # Assignment through the uniform PATCH, to the logged-in user.
            me = wf.get_current_user(session_id=session_id)
            assigned = wf.assign_item(TEST_PROJECT, b["ref"], me["email"], session_id=session_id)
            assert assigned["assigned_to"]

            # Wiki: create, then upsert again to take the update path.
            slug = f"test-can-delete-{tag}"
            first = wf.upsert_wiki(TEST_PROJECT, slug, "v1", session_id=session_id)
            made["wiki"].append(first["id"])
            assert first["status"] == "created"
            second = wf.upsert_wiki(TEST_PROJECT, slug, "v2", session_id=session_id)
            assert second["status"] == "updated"
            assert wf.get_wiki(TEST_PROJECT, slug, session_id=session_id)["content"] == "v2"
        finally:
            for resource in ("epics", "issues", "wiki", "userstories", "milestones"):
                for item_id in made[resource]:
                    _delete(session_id, resource, item_id)
        stories_after = len(
            wf.active_sessions[session_id].list_resources("user_stories", project_id=project_id)
        )
        assert stories_after == stories_before, "a story was left behind (cf. #143 junk story)"
        assert api.auth_token

    def test_b5_list_update_delete_on_real_taiga(self, session_id):
        """v3 B5 on real endpoints. Filters must EXCLUDE (Taiga ignores unknown params
        silently), deletes must have the side effects the tool description states,
        and cleanup goes through delete_item itself (raw delete as a fallback)."""
        sess = wf.active_sessions[session_id]
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        tag = uuid.uuid4().hex[:8]
        stories_before = len(sess.list_resources("user_stories", project_id=project_id))
        made = {"userstories": [], "tasks": [], "epics": [], "milestones": [], "wiki": []}
        me = wf.get_current_user(session_id=session_id)
        try:
            sprint = f"[TEST] b5 {tag}"
            planned = wf.plan_sprint(
                TEST_PROJECT, sprint, "2026-01-01", "2026-01-14", session_id=session_id
            )
            made["milestones"].append(planned["sprint"]["id"])
            a = wf.create_story(
                TEST_PROJECT,
                f"[TEST - CAN DELETE] a {tag}",
                sprint=sprint,
                tags=[f"t{tag}"],
                assignee=me["email"],
                session_id=session_id,
            )
            b = wf.create_story(TEST_PROJECT, f"[TEST - CAN DELETE] b {tag}", session_id=session_id)
            made["userstories"] += [a["id"], b["id"]]
            task = wf.create_task(
                TEST_PROJECT, b["ref"], f"[TEST] task {tag}", session_id=session_id
            )
            made["tasks"].append(task["id"])
            epic = wf.create_epic(
                TEST_PROJECT,
                f"[TEST - CAN DELETE] epic {tag}",
                story_refs=[a["ref"]],
                session_id=session_id,
            )
            made["epics"].append(epic["id"])

            # #144 acceptance: a sprint-assigned story is in list_items, not in browse_backlog.
            all_refs = {s["ref"] for s in wf.list_items(TEST_PROJECT, session_id=session_id)}
            backlog = {s["ref"] for s in wf.browse_backlog(TEST_PROJECT, session_id=session_id)}
            assert a["ref"] in all_refs and a["ref"] not in backlog
            assert b["ref"] in all_refs and b["ref"] in backlog

            def refs(**kw):
                return {s["ref"] for s in wf.list_items(TEST_PROJECT, session_id=session_id, **kw)}

            # Story filters really exclude b.
            assert refs(sprint=sprint) == {a["ref"]}
            assert refs(epic=epic["ref"]) == {a["ref"]}
            assert refs(tags=f"t{tag}") == {a["ref"]}
            assert a["ref"] in refs(assignee=me["email"]) and b["ref"] not in refs(
                assignee=me["email"]
            )

            # update_epic: status resolved by name via epic_statuses, then filter by it.
            statuses = [
                s["name"] for s in sess.list_resources("epic_statuses", project_id=project_id)
            ]
            target = statuses[-1]
            updated = wf.update_epic(
                TEST_PROJECT,
                epic["ref"],
                subject=f"[TEST - CAN DELETE] epic {tag} v2",
                status=target,
                assignee=me["email"],
                session_id=session_id,
            )
            assert updated["subject"].endswith("v2") and updated["status"] == target

            def epic_refs(**kw):
                return {
                    e["ref"]
                    for e in wf.list_items(
                        TEST_PROJECT, entity_type="epic", session_id=session_id, **kw
                    )
                }

            assert epic["ref"] in epic_refs(status=target)
            if len(statuses) > 1:
                assert epic["ref"] not in epic_refs(status=statuses[0])
            assert epic["ref"] in epic_refs(assignee=me["email"])

            # Sprint filter by open/closed.
            def sprint_names(status):
                rows = wf.list_items(
                    TEST_PROJECT, entity_type="sprint", status=status, session_id=session_id
                )
                return {s["name"] for s in rows}

            assert sprint in sprint_names("open") and sprint not in sprint_names("closed")

            # Deletes, with the side effects the description states.
            d = wf.delete_item(TEST_PROJECT, "sprint", sprint, session_id=session_id)
            assert d["name"] == sprint
            made["milestones"].clear()
            assert wf.get_story(TEST_PROJECT, a["ref"], session_id=session_id)["sprint"] is None

            d = wf.delete_item(TEST_PROJECT, "epic", epic["ref"], session_id=session_id)
            assert d["subject"].endswith("v2")
            made["epics"].clear()
            assert wf.get_story(TEST_PROJECT, a["ref"], session_id=session_id)  # story kept

            d = wf.delete_item(TEST_PROJECT, "story", b["ref"], session_id=session_id)
            assert d["subject"] == f"[TEST - CAN DELETE] b {tag}"
            made["userstories"].remove(b["id"])
            with pytest.raises(TaigaNotFoundError):  # by_ref 404: deleted with its story
                wf.get_task(TEST_PROJECT, task["ref"], session_id=session_id)
            made["tasks"].clear()

            slug = f"test-can-delete-{tag}"
            made["wiki"].append(
                wf.upsert_wiki(TEST_PROJECT, slug, "x", session_id=session_id)["id"]
            )
            assert wf.delete_item(TEST_PROJECT, "wiki", slug, session_id=session_id)["slug"] == slug
            made["wiki"].clear()
            with pytest.raises(ValueError, match="not found"):
                wf.delete_item(TEST_PROJECT, "wiki", slug, session_id=session_id)

            wf.delete_item(TEST_PROJECT, "story", a["ref"], session_id=session_id)
            made["userstories"].remove(a["id"])
        finally:
            for resource in ("tasks", "epics", "wiki", "userstories", "milestones"):
                for item_id in made[resource]:
                    _delete(session_id, resource, item_id)
        stories_after = len(sess.list_resources("user_stories", project_id=project_id))
        assert stories_after == stories_before
