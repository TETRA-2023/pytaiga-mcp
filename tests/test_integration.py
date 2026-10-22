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

import src.server as wf
from src.taiga_client import TaigaBadRequestError

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
            # Deleted with its story. Since tetra #2658 the by_ref 404 is a clear
            # "not found" ValueError, not a raw TaigaNotFoundError.
            with pytest.raises(ValueError, match="not found"):
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
            # A tag outlives the items that used it in the project's tag list.
            sess.api.post(f"/projects/{project_id}/delete_tag", json={"tag": f"t{tag}"})
        stories_after = len(sess.list_resources("user_stories", project_id=project_id))
        assert stories_after == stories_before

    def test_b6_comments_and_history_on_real_taiga(self, session_id):
        """v3 B6 (#154): read comments in full, edit (with versions), delete, restore."""
        tag = uuid.uuid4().hex[:8]
        story = wf.create_story(
            TEST_PROJECT, f"[TEST - CAN DELETE] b6 {tag}", session_id=session_id
        )
        try:
            long_text = f"decision {tag}\n" + "x" * 400
            wf.add_comment(TEST_PROJECT, story["ref"], long_text, session_id=session_id)
            wf.update_story(TEST_PROJECT, story["ref"], description="edited", session_id=session_id)

            comments = wf.list_comments(TEST_PROJECT, story["ref"], session_id=session_id)
            assert len(comments) == 1 and comments[0]["comment"] == long_text  # full text
            cid = comments[0]["id"]

            wf.edit_comment(
                TEST_PROJECT, story["ref"], cid, f"amended {tag}", session_id=session_id
            )
            history = wf.get_history(
                TEST_PROJECT, story["ref"], include_comment_versions=True, session_id=session_id
            )
            assert any("description" in e["changed_fields"] for e in history)
            entry = next(e for e in history if e["id"] == cid)
            assert entry["comment"] == f"amended {tag}" and entry["comment_edited_at"]
            assert [v["comment"] for v in entry["comment_versions"]] == [long_text]

            wf.delete_comment(TEST_PROJECT, story["ref"], cid, session_id=session_id)
            assert wf.list_comments(TEST_PROJECT, story["ref"], session_id=session_id) == []
            hidden = wf.list_comments(
                TEST_PROJECT, story["ref"], include_deleted=True, session_id=session_id
            )
            assert [c["id"] for c in hidden] == [cid] and hidden[0]["deleted_at"]

            wf.restore_comment(TEST_PROJECT, story["ref"], cid, session_id=session_id)
            restored = wf.list_comments(TEST_PROJECT, story["ref"], session_id=session_id)
            assert [c["comment"] for c in restored] == [f"amended {tag}"]

            with pytest.raises(ValueError, match="not found"):
                wf.delete_comment(
                    TEST_PROJECT,
                    story["ref"],
                    "00000000-0000-0000-0000-000000000000",
                    session_id=session_id,
                )
        finally:
            try:
                wf.delete_item(TEST_PROJECT, "story", story["ref"], session_id=session_id)
            except Exception:
                _delete(session_id, "userstories", story["id"])

    def test_b6_long_history_is_not_paginated(self, session_id):
        """Taiga pages /history at 30 entries: without x-disable-pagination an item
        with a long history silently lost its OLDEST comments (verified: 30 of 35)."""
        tag = uuid.uuid4().hex[:8]
        story = wf.create_story(
            TEST_PROJECT, f"[TEST - CAN DELETE] long {tag}", session_id=session_id
        )
        try:
            for i in range(35):
                wf.add_comment(TEST_PROJECT, story["ref"], f"c{i} {tag}", session_id=session_id)
            comments = wf.list_comments(TEST_PROJECT, story["ref"], session_id=session_id)
            assert len(comments) == 35
            assert comments[0]["comment"] == f"c0 {tag}"  # the oldest one survives
            assert len(wf.get_history(TEST_PROJECT, story["ref"], session_id=session_id)) >= 35
        finally:
            try:
                wf.delete_item(TEST_PROJECT, "story", story["ref"], session_id=session_id)
            except Exception:
                _delete(session_id, "userstories", story["id"])

    def test_b7_attachments_and_tags_on_real_taiga(self, session_id):
        """v3 B7: multipart upload round trip (bytes compared), ownership guard on delete,
        and tag rename/merge (rva is project admin on the smartagile sandbox)."""
        import base64

        import httpx

        tag = uuid.uuid4().hex[:8]
        api = wf.active_sessions[session_id].api
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        a = wf.create_story(
            TEST_PROJECT,
            f"[TEST - CAN DELETE] b7a {tag}",
            tags=[f"x{tag}", f"y{tag}"],
            session_id=session_id,
        )
        b = wf.create_story(TEST_PROJECT, f"[TEST - CAN DELETE] b7b {tag}", session_id=session_id)
        try:
            content = f"attachment {tag} ✓\n".encode()
            added = wf.add_attachment(
                TEST_PROJECT,
                a["ref"],
                f"probe-{tag}.txt",
                base64.b64encode(content).decode(),
                description="b7",
                session_id=session_id,
            )
            assert added["status"] == "attached" and added["size"] == len(content)
            listed = wf.list_attachments(TEST_PROJECT, a["ref"], session_id=session_id)
            assert [x["name"] for x in listed] == [f"probe-{tag}.txt"]
            assert httpx.get(listed[0]["url"], timeout=30, follow_redirects=True).content == content

            with pytest.raises(ValueError, match="not attached"):  # wrong item: nothing deleted
                wf.delete_attachment(TEST_PROJECT, b["ref"], added["id"], session_id=session_id)
            assert len(wf.list_attachments(TEST_PROJECT, a["ref"], session_id=session_id)) == 1
            wf.delete_attachment(TEST_PROJECT, a["ref"], added["id"], session_id=session_id)
            assert wf.list_attachments(TEST_PROJECT, a["ref"], session_id=session_id) == []

            tags = {t["tag"] for t in wf.list_tags(TEST_PROJECT, session_id=session_id)}
            assert {f"x{tag}", f"y{tag}"} <= tags
            renamed = wf.rename_tag(
                TEST_PROJECT, f"x{tag}", new_name=f"z{tag}", color="#A5694F", session_id=session_id
            )
            assert renamed["color"] == "#A5694F"
            colors = {
                t["tag"]: t["color"] for t in wf.list_tags(TEST_PROJECT, session_id=session_id)
            }
            assert colors.get(f"z{tag}") == "#A5694F" and f"x{tag}" not in colors
            wf.merge_tags(TEST_PROJECT, [f"y{tag}"], f"z{tag}", session_id=session_id)
            assert [t[0] for t in api.get(f"/userstories/{a['id']}")["tags"]] == [f"z{tag}"]
            with pytest.raises(TaigaBadRequestError, match="doesn't exist"):
                wf.rename_tag(TEST_PROJECT, f"nope{tag}", new_name="q", session_id=session_id)
        finally:
            for story in (a, b):
                try:
                    wf.delete_item(TEST_PROJECT, "story", story["ref"], session_id=session_id)
                except Exception:
                    _delete(session_id, "userstories", story["id"])
            for t in [t for t in api.get(f"/projects/{project_id}/tags_colors") if tag in t]:
                api.post(f"/projects/{project_id}/delete_tag", json={"tag": t})

    def test_b8_project_admin_on_real_taiga(self, session_id):
        """v3 B8 as project admin (rva on the smartagile sandbox). Sends NO invitation:
        invite_members is exercised on its refusal paths only."""
        tag = uuid.uuid4().hex[:6]
        api = wf.active_sessions[session_id].api
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        original = [
            s["name"]
            for s in wf.list_project_settings(TEST_PROJECT, "story_statuses", session_id=session_id)
        ]
        first = original[0]
        status_name, point_name, attr_name = f"TEST st {tag}", f"T{tag}", f"TEST attr {tag}"
        story = wf.create_story(
            TEST_PROJECT, f"[TEST - CAN DELETE] b8 {tag}", session_id=session_id
        )
        made = []  # (endpoint, id) for the raw fallback cleanup
        try:
            members = wf.list_members(TEST_PROJECT, session_id=session_id)
            me = wf.get_current_user(session_id=session_id)["email"]
            assert any(m["email"] == me for m in members["members"]) and members["roles"]
            with pytest.raises(ValueError, match="Already members"):
                wf.invite_members(
                    TEST_PROJECT, [me.upper()], members["roles"][0], session_id=session_id
                )
            with pytest.raises(ValueError, match="No role named"):
                wf.invite_members(
                    TEST_PROJECT, [f"nobody-{tag}@example.invalid"], "Nope", session_id=session_id
                )

            created = wf.create_project_setting(
                TEST_PROJECT, "story_statuses", status_name, color="#123456", session_id=session_id
            )
            made.append(("userstory-statuses", created["id"]))
            # Cache invalidation: the new status resolves by name in the same session.
            moved = wf.set_story_status(
                TEST_PROJECT, story["ref"], status_name, session_id=session_id
            )
            assert moved["status"] == status_name
            wf.update_project_setting(
                TEST_PROJECT,
                "story_statuses",
                status_name,
                new_name=f"{status_name} v2",
                color="#654321",
                session_id=session_id,
            )
            status_name = f"{status_name} v2"

            wf.reorder_project_settings(
                TEST_PROJECT, "story_statuses", [status_name, *original], session_id=session_id
            )
            names = [
                s["name"]
                for s in wf.list_project_settings(
                    TEST_PROJECT, "story_statuses", session_id=session_id
                )
            ]
            assert names[0] == status_name
            wf.reorder_project_settings(
                TEST_PROJECT, "story_statuses", [*original, status_name], session_id=session_id
            )

            with pytest.raises(ValueError, match="needs move_to"):
                wf.delete_project_setting(
                    TEST_PROJECT, "story_statuses", status_name, session_id=session_id
                )
            deleted = wf.delete_project_setting(
                TEST_PROJECT, "story_statuses", status_name, move_to=first, session_id=session_id
            )
            made.remove(("userstory-statuses", created["id"]))
            assert deleted["moved_to"] == first
            assert (
                wf.get_story(TEST_PROJECT, story["ref"], session_id=session_id)["status"] == first
            )

            point = wf.create_project_setting(
                TEST_PROJECT, "points", point_name, value=21.5, session_id=session_id
            )
            made.append(("points", point["id"]))
            assert point["value"] == 21.5
            wf.delete_project_setting(
                TEST_PROJECT, "points", point_name, move_to="?", session_id=session_id
            )
            made.remove(("points", point["id"]))

            attr = wf.create_project_setting(
                TEST_PROJECT,
                "story_custom_attributes",
                attr_name,
                description="b8",
                field_type="text",
                session_id=session_id,
            )
            made.append(("userstory-custom-attributes", attr["id"]))
            assert attr["field_type"] == "text"
            result = wf.set_custom_attribute_values(
                TEST_PROJECT, story["ref"], {attr_name: "hello"}, session_id=session_id
            )
            assert result["values"] == {attr_name: "hello"}
            cleared = wf.set_custom_attribute_values(
                TEST_PROJECT, story["ref"], {attr_name: None}, session_id=session_id
            )
            assert cleared["values"][attr_name] is None  # cleared, and Taiga accepted it
            wf.delete_project_setting(
                TEST_PROJECT, "story_custom_attributes", attr_name, session_id=session_id
            )
            made.remove(("userstory-custom-attributes", attr["id"]))
        finally:
            for endpoint, item_id in made:
                params = None
                if endpoint != "userstory-custom-attributes":
                    rows = api.get(
                        f"/{endpoint}",
                        params={"project": project_id},
                        headers={"x-disable-pagination": "True"},
                    )
                    keep = next(r for r in rows if r["id"] != item_id)
                    params = {"moveTo": keep["id"]}
                try:
                    api.delete(f"/{endpoint}/{item_id}", params=params)
                except Exception as e:
                    print(f"cleanup: {endpoint}/{item_id}: {e}")
            try:
                wf.delete_item(TEST_PROJECT, "story", story["ref"], session_id=session_id)
            except Exception:
                _delete(session_id, "userstories", story["id"])
        after = [
            s["name"]
            for s in wf.list_project_settings(TEST_PROJECT, "story_statuses", session_id=session_id)
        ]
        assert after == original, "the sandbox's status order was not restored"

    def test_b9_bulk_link_reorder_on_real_taiga(self, session_id):
        """v3 B9: create_items, link_to_epic (already-linked), reorder_stories in the backlog,
        a sprint and kanban, and the guard that keeps a sprint story where it is."""
        tag = uuid.uuid4().hex[:6]
        sess = wf.active_sessions[session_id]
        project_id = wf.get_project_overview(TEST_PROJECT, session_id=session_id)["project"]["id"]
        before_count = len(sess.list_resources("user_stories", project_id=project_id))
        refs, epic, sprint_id = [], None, None
        try:
            made = wf.create_items(
                TEST_PROJECT,
                [f"[TEST - CAN DELETE] b9-{i} {tag}" for i in range(4)],
                session_id=session_id,
            )
            assert made["count"] == 4
            refs = [i["ref"] for i in made["items"]]

            epic = wf.create_items(
                TEST_PROJECT,
                [f"[TEST - CAN DELETE] b9 epic {tag}"],
                entity_type="epic",
                session_id=session_id,
            )["items"][0]
            first = wf.link_to_epic(TEST_PROJECT, epic["ref"], refs[:2], session_id=session_id)
            assert first["linked"] == refs[:2] and first["already_linked"] == []
            again = wf.link_to_epic(TEST_PROJECT, epic["ref"], refs[1:3], session_id=session_id)
            assert again == {
                "status": "linked",
                "epic_ref": epic["ref"],
                "linked": [refs[2]],
                "already_linked": [refs[1]],
            }
            overview = wf.get_epic_overview(TEST_PROJECT, epic["ref"], session_id=session_id)
            assert sorted(s["ref"] for s in overview["stories"]) == sorted(refs[:3])

            def order(field):
                rows = sess.list_resources("user_stories", project_id=project_id)
                by_ref = {r["ref"]: r[field] for r in rows if r["ref"] in refs}
                return sorted((r for r in refs if r in by_ref), key=lambda r: by_ref[r])

            # Backlog: the last two right AFTER the first. Without an anchor Taiga puts
            # them at the top ([3, 2, 0, 1]), so this order tells the two apart.
            wf.reorder_stories(
                TEST_PROJECT, [refs[3], refs[2]], after_ref=refs[0], session_id=session_id
            )
            assert order("backlog_order") == [refs[0], refs[3], refs[2], refs[1]]

            # A story inside a sprint is refused (the endpoint would pull it out).
            sprint = f"[TEST] b9 {tag}"
            planned = wf.plan_sprint(
                TEST_PROJECT,
                sprint,
                "2026-01-01",
                "2026-01-14",
                story_refs=refs[:2],
                session_id=session_id,
            )
            sprint_id = planned["sprint"]["id"]
            with pytest.raises(ValueError, match="not in the backlog"):
                wf.reorder_stories(TEST_PROJECT, [refs[0], refs[2]], session_id=session_id)
            assert wf.get_story(TEST_PROJECT, refs[0], session_id=session_id)["sprint"] == sprint

            # Sprint view: the first AFTER the second (top would keep it first).
            wf.reorder_stories(
                TEST_PROJECT,
                [refs[0]],
                view="sprint",
                sprint=sprint,
                after_ref=refs[1],
                session_id=session_id,
            )
            sprint_order = [r for r in order("sprint_order") if r in refs[:2]]
            assert sprint_order == [refs[1], refs[0]]

            # Kanban (the two backlog stories share the default status): refs[2] AFTER
            # refs[3]. They were created 2-then-3, and top would keep 2 first.
            wf.reorder_stories(
                TEST_PROJECT, [refs[2]], view="kanban", after_ref=refs[3], session_id=session_id
            )
            kanban = [r for r in order("kanban_order") if r in refs[2:]]
            assert kanban == [refs[3], refs[2]]
        finally:
            for ref in refs:
                try:
                    wf.delete_item(TEST_PROJECT, "story", ref, session_id=session_id)
                except Exception as e:
                    print(f"cleanup: story #{ref}: {e}")
            if epic:
                try:
                    wf.delete_item(TEST_PROJECT, "epic", epic["ref"], session_id=session_id)
                except Exception:
                    _delete(session_id, "epics", epic["id"])
            if sprint_id:
                _delete(session_id, "milestones", sprint_id)
        assert len(sess.list_resources("user_stories", project_id=project_id)) == before_count
