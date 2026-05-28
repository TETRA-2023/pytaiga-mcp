import os
import time
import uuid

import pytest

import src.server_full
from src.server_full import (
    add_comment,
    create_user_story,
    delete_comment,
    edit_comment,
    get_comment_versions,
    get_project,
    list_comments,
    list_projects,
    list_user_stories,
    login,
    undelete_comment,
    update_project,
)

# Test constants - use environment variables or defaults for testing
TEST_HOST = os.environ.get(
    "TAIGA_TEST_HOST", os.environ.get("TAIGA_API_URL", "http://localhost:9000")
)
TEST_USERNAME = os.environ.get("TAIGA_TEST_USERNAME", os.environ.get("TAIGA_USERNAME", "test"))
TEST_PASSWORD = os.environ.get("TAIGA_TEST_PASSWORD", os.environ.get("TAIGA_PASSWORD", "test"))


@pytest.mark.integration  # Mark these to run separately
class TestTaigaIntegration:
    @pytest.fixture
    def session_id(self):
        """Create a real session"""
        result = login(TEST_HOST, TEST_USERNAME, TEST_PASSWORD)
        return result["session_id"]

    def test_project_access(self, session_id):
        """Test access to projects using real API calls"""
        # 1. List projects
        projects = list_projects(session_id)

        # Verify we got at least one project
        assert len(projects) > 0, "No projects found in your Taiga account"

        # Prefer Project ID 10 (known working), otherwise avoid corrupted ID 9
        project_id = next((p["id"] for p in projects if p["id"] == 10), None)
        if not project_id:
            project_id = next((p["id"] for p in projects if p["id"] != 9), projects[0]["id"])

        # 2. Get details of a specific project - note: project_id first, then session_id
        project = get_project(project_id, session_id)
        assert project["id"] == project_id

        # Store the original name
        original_name = project["name"]

        try:
            # 3. Update the project with a timestamp - note: project_id first, then kwargs dict, then session_id
            new_name = f"{original_name} (Test {time.time()})"
            updated = update_project(project_id, {"name": new_name}, session_id)
            assert updated["name"] == new_name

        finally:
            # 4. Restore the original name
            update_project(project_id, {"name": original_name}, session_id)

    def test_user_story_workflow(self, session_id):
        """Test user story creation and listing with real API calls"""
        # Get the first project for testing
        projects = list_projects(session_id)
        print(f"DEBUG: Found {len(projects)} projects")  # DEBUG
        assert len(projects) > 0, "No projects found in your Taiga account"

        # Prefer Project ID 10 (known working), otherwise avoid corrupted ID 9
        project_id = next((p["id"] for p in projects if p["id"] == 10), None)
        if not project_id:
            project_id = next((p["id"] for p in projects if p["id"] != 9), projects[0]["id"])
        print(f"DEBUG: Using project ID {project_id}")  # DEBUG

        # Create a user story with unique subject
        subject = f"Test Story {uuid.uuid4()}"
        description = "Integration test user story"

        # Create the user story - note: project_id, subject, kwargs dict, session_id
        story = create_user_story(project_id, subject, {"description": description}, session_id)
        print(f"DEBUG: Created story: {story['id']} - {story['subject']}")  # DEBUG
        story_id = story["id"]

        try:
            # Get the list of user stories and verify our story is there - note: project_id first
            time.sleep(1)  # Small delay to ensure creation is complete
            stories = list_user_stories(project_id, None, session_id)

            found = False
            for s in stories:
                if s["id"] == story_id:
                    found = True
                    assert s["subject"] == subject
                    break

            assert found, "Created user story not found in stories list"

        finally:
            # Clean up - mark it as a test that can be ignored/deleted manually
            # Note: story_id, kwargs dict, session_id
            if "update_user_story" in dir(src.server):
                src.server.update_user_story(
                    story_id, {"subject": f"[TEST - CAN DELETE] {subject}"}, session_id
                )

    def test_comment_lifecycle(self, session_id):
        """End-to-end: add → list → edit → versions → delete → list → undelete → list.

        Regression test for the query-string-vs-body bug on Taiga's history endpoints
        (delete_comment, undelete_comment, edit_comment, comment_versions). Mocked
        unit tests cannot catch this because they do not observe the 404 Taiga returns
        when the `id` query param is missing.
        """
        projects = list_projects(session_id)
        assert len(projects) > 0, "No projects found in your Taiga account"

        project_id = next((p["id"] for p in projects if p["id"] == 10), None)
        if not project_id:
            project_id = next((p["id"] for p in projects if p["id"] != 9), projects[0]["id"])

        subject = f"[TEST - CAN DELETE] comment lifecycle {uuid.uuid4()}"
        story = create_user_story(project_id, subject, None, session_id)
        story_id = story["id"]

        original_text = f"first body {uuid.uuid4()}"
        edited_text = f"edited body {uuid.uuid4()}"

        try:
            add_comment(story_id, "user_story", original_text, session_id)
            time.sleep(1)  # let the history entry settle

            comments = list_comments(story_id, "user_story", session_id)
            ours = [c for c in comments if c["comment"] == original_text]
            assert len(ours) == 1, f"Expected 1 new comment, got {len(ours)}"
            comment_id = ours[0]["id"]

            edit_result = edit_comment(story_id, "user_story", comment_id, edited_text, session_id)
            assert edit_result["status"] == "comment_edited"

            versions = get_comment_versions(story_id, "user_story", comment_id, session_id)
            assert versions["comment_id"] == comment_id
            assert len(versions["versions"]) >= 1, "Edit should have produced a version entry"

            comments = list_comments(story_id, "user_story", session_id)
            assert any(c["id"] == comment_id and c["comment"] == edited_text for c in comments), (
                "Edited comment text not visible in list_comments"
            )

            delete_result = delete_comment(story_id, "user_story", comment_id, session_id)
            assert delete_result["status"] == "comment_deleted"

            comments = list_comments(story_id, "user_story", session_id)
            assert not any(c["id"] == comment_id for c in comments), (
                "Deleted comment should not appear in list_comments"
            )

            undelete_result = undelete_comment(story_id, "user_story", comment_id, session_id)
            assert undelete_result["status"] == "comment_restored"

            comments = list_comments(story_id, "user_story", session_id)
            assert any(c["id"] == comment_id for c in comments), (
                "Restored comment should be visible again in list_comments"
            )
        finally:
            # Match the existing pattern in this file: leave the story but mark it
            # as a throwaway so maintainers can clean up manually later.
            if "update_user_story" in dir(src.server):
                src.server.update_user_story(
                    story_id, {"subject": f"[TEST - CAN DELETE] {subject}"}, session_id
                )
