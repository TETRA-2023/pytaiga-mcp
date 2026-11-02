"""Project-level reads: projects, overview, search, workload, health, activity."""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Union

from src.server import app, core, resolve
from src.taiga_client import TaigaNotFoundError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@app.tool(
    "list_projects",
    description="List all Taiga projects accessible to the authenticated user.",
)
def list_projects(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_list():
        projects = client.list_resources("projects")
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "slug": p["slug"],
                "description": p.get("description"),
                "is_private": p.get("is_private"),
            }
            for p in projects
        ]

    return core._execute_taiga_operation("list_projects", do_list)


@app.tool(
    "get_project_overview",
    description=(
        "Return a full snapshot of a project: description, team, active sprint, "
        "and user story counts by status. project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_overview(
    project: Union[str, int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_overview():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        today = date.today().isoformat()

        # Team
        members = client.list_resources("memberships", project_id=project_id)
        # Memberships carry NO user_extra_info (and no username at all). Identity
        # comes from flat fields: `user_email` is the reliable one — `email` is
        # often blank on established members. Email is also what user resolution
        # matches on, so it is the useful handle to surface here.
        team = [
            {
                "email": m.get("user_email") or m.get("email"),
                "full_name": m.get("full_name"),
                "role": m.get("role_name"),
                "is_admin": m.get("is_admin", False),
            }
            for m in members
        ]

        # Active sprint
        milestones = client.list_resources("milestones", project_id=project_id)
        current_sprints = [
            m
            for m in milestones
            if not m.get("closed")
            and (m.get("estimated_start") or "9999-12-31")
            <= today
            <= (m.get("estimated_finish") or "0000-01-01")
        ]
        active_sprint = None
        if len(current_sprints) == 1:
            m = current_sprints[0]
            active_sprint = {
                "name": m["name"],
                "start": m.get("estimated_start"),
                "end": m.get("estimated_finish"),
                "id": m["id"],
            }

        # Story counts by status
        stories = client.list_resources("user_stories", project_id=project_id)
        by_status: Dict[str, int] = {}
        for s in stories:
            status_name = (s.get("status_extra_info") or {}).get("name") or str(s.get("status"))
            by_status[status_name] = by_status.get(status_name, 0) + 1

        return {
            "project": {
                "id": proj["id"],
                "name": proj["name"],
                "slug": proj["slug"],
                "description": proj.get("description"),
                "is_private": proj.get("is_private"),
            },
            "team": team,
            "active_sprint": active_sprint,
            "stories_by_status": by_status,
            "total_stories": len(stories),
        }

    return core._execute_taiga_operation("get_project_overview", do_overview, str(project))


@app.tool(
    "search",
    description=(
        "Full-text search within a project across user stories, tasks, issues, epics, and wiki pages. "
        "Returns grouped results. project accepts slug or ID."
    ),
)
def search(
    project: Union[str, int],
    query: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_search():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        result = client.api.get("/search", params={"project": project_id, "text": query})

        # Taiga's /search echoes status as a raw numeric id with no name or
        # is_closed flag (issue #95). Resolve each entity group's status against
        # the project's status table so reads are self-interpreting and match the
        # write tools' flat {status: <name>, is_closed: <bool>} shape.
        def _resolved(group_key: str, entity_type: str) -> List[Dict[str, Any]]:
            items = result.get(group_key, []) or []
            if not items:
                return []
            status_by_id = resolve._status_map(client, project_id, entity_type, actual_session_id)
            for item in items:
                info = status_by_id.get(item.get("status"))
                if info is not None:
                    # Known status: the status table is authoritative.
                    item["status"] = info["name"]
                    item["is_closed"] = info["is_closed"]
                else:
                    # Unknown id: keep the raw status and don't clobber any
                    # is_closed the payload already carries; default to None so
                    # the key is still present and the output shape stays uniform.
                    item.setdefault("is_closed", None)
            return items

        return {
            "query": query,
            "project": proj["slug"],
            "count": result.get("count", 0),
            "user_stories": _resolved("userstories", "story"),
            "tasks": _resolved("tasks", "task"),
            "issues": _resolved("issues", "issue"),
            "epics": _resolved("epics", "epic"),
            "wiki_pages": result.get("wikipages", []),
        }

    return core._execute_taiga_operation("search", do_search, f"project={project} query={query!r}")


# ---------------------------------------------------------------------------
# Team tools
# ---------------------------------------------------------------------------


@app.tool(
    "get_team_workload",
    description=(
        "Show per-member story and task counts for a sprint or the full project backlog. "
        "sprint accepts name or ID; omit for whole-project view. project accepts slug or ID."
    ),
)
def get_team_workload(
    project: Union[str, int],
    sprint: Optional[Union[str, int]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_workload():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        filters: Dict[str, Any] = {}
        sprint_name = None
        if sprint is not None:
            milestone = resolve._resolve_sprint(client, project_id, sprint)
            filters["milestone"] = milestone["id"]
            sprint_name = milestone["name"]

        stories = client.list_resources("user_stories", project_id=project_id, **filters)
        tasks = client.list_resources("tasks", project_id=project_id, **filters)

        # Aggregate per assignee
        workload: Dict[str, Dict[str, Any]] = {}

        def _add(name: Optional[str], category: str):
            if not name:
                name = "(unassigned)"
            if name not in workload:
                workload[name] = {"stories": 0, "tasks": 0, "blocked_stories": 0}
            workload[name][category] += 1

        for s in stories:
            name = (s.get("assigned_to_extra_info") or {}).get("full_name_display")
            _add(name, "stories")
            if s.get("is_blocked"):
                _add(name, "blocked_stories")

        for t in tasks:
            name = (t.get("assigned_to_extra_info") or {}).get("full_name_display")
            _add(name, "tasks")

        return {
            "project": proj["slug"],
            "sprint": sprint_name,
            "team": [{"member": name, **counts} for name, counts in sorted(workload.items())],
        }

    return core._execute_taiga_operation("get_team_workload", do_workload, str(project))


# ---------------------------------------------------------------------------
# Project health tool
# ---------------------------------------------------------------------------


@app.tool(
    "get_project_health",
    description=(
        "Return a metrics snapshot for a project: story point completion, computed velocity speed "
        "(points/day), sprint-by-sprint velocity history, open issue counts by priority, and which "
        "modules are active. "
        "Complements get_project_overview (structure) with quantitative health signals. "
        "project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_health(
    project: Union[str, int],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    def do_health():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]

        stats = client.api.get(f"/projects/{project_id}/stats") or {}
        issues_stats = client.api.get(f"/projects/{project_id}/issues_stats") or {}
        modules = client.api.get(f"/projects/{project_id}/modules") or {}

        # Sprint velocity: list of closed_points per milestone (oldest → newest)
        milestones = stats.get("milestones") or []
        velocity = [
            {"sprint": m.get("name"), "closed_points": m.get("closed_points", 0)}
            for m in milestones
        ]

        # Issues breakdown by priority
        issues_by_priority = {
            entry.get("name", "unknown"): entry.get("count", 0)
            for entry in (issues_stats.get("issues_per_priority") or [])
        }

        # Active modules: slice off "is_" prefix (3 chars) and "_activated" suffix (10 chars)
        active_modules = [
            name[3:-10]
            for name, enabled in modules.items()
            if name.startswith("is_") and name.endswith("_activated") and enabled
        ]

        return {
            "project": {"id": proj["id"], "name": proj["name"], "slug": proj["slug"]},
            "stories": {
                "total_points": stats.get("total_points", 0),
                "closed_points": stats.get("closed_points", 0),
                "assigned_points": stats.get("assigned_points", 0),
                "total_milestones": stats.get("total_milestones", 0),
                "speed": stats.get("speed", 0),
            },
            "issues": {
                "total": issues_stats.get("total_issues", 0),
                "open": issues_stats.get("opened_issues", 0),
                "closed": issues_stats.get("closed_issues", 0),
                "by_priority": issues_by_priority,
            },
            "velocity": velocity,
            "active_modules": active_modules,
        }

    return core._execute_taiga_operation("get_project_health", do_health, str(project))


# ---------------------------------------------------------------------------
# Project activity tool
# ---------------------------------------------------------------------------

# Map Taiga timeline event_type prefixes to readable entity labels.
_TIMELINE_ENTITY_MAP = {
    "userstories": "user_story",
    "tasks": "task",
    "issues": "issue",
    "epics": "epic",
    "milestones": "sprint",
    "wiki": "wiki_page",
    "projects": "project",
}


# get_project_activity(entity=...): user-facing type -> timeline event_type prefix.
_ACTIVITY_ENTITY_FILTER = {
    "story": "userstories",
    "task": "tasks",
    "issue": "issues",
    "epic": "epics",
    "sprint": "milestones",
    "wiki": "wiki",
}

# Ticket-row subjects are shortened to this when a summary is too large.
_ACTIVITY_SHORT_SUBJECT = 60

# Where a timeline event names its item ({id, ref, subject}), most specific first:
# a task event also carries its story.
_TIMELINE_ITEM_KEYS = ("task", "issue", "userstory", "epic")

# get_project_activity(since=...) reads the timeline this many events per page,
# and stops (truncated) after scanning this many, matched by the filters or not.
_ACTIVITY_PAGE_SIZE = 100
_ACTIVITY_MAX_SCAN = 5000

# Above this many characters of JSON an agent client stores the result in a file
# instead of reading it (T2.9: 438 events = 143,000 characters), so the result is
# summarised per ticket, then cut.
_ACTIVITY_MAX_CHARS = 50_000

# Activity comments longer than this are shortened unless full_comments=True.
_ACTIVITY_COMMENT_CHARS = 200


def _summarize_timeline_event(event: Dict[str, Any], full_comments: bool = False) -> Dict[str, Any]:
    """Distil a raw Taiga timeline event into a readable summary dict."""
    event_type = event.get("event_type", "")
    parts = event_type.split(".")
    entity = _TIMELINE_ENTITY_MAP.get(parts[0], parts[0]) if parts[0] else "unknown"
    action = parts[-1] if len(parts) >= 2 else "unknown"

    data = event.get("data") or {}
    user_info = data.get("user") or {}
    actor = user_info.get("name") or user_info.get("username") or "unknown"

    values_diff = data.get("values_diff") or {}
    changed_fields = list(values_diff.keys()) if values_diff else []

    comment = data.get("comment") or ""

    summary: Dict[str, Any] = {
        "when": event.get("created"),
        "actor": actor,
        "entity": entity,
        "action": action,
        # No object_id: the timeline is namespaced per project, so it is the
        # PROJECT's id. The item is named by ref/subject below.
    }
    for key in _TIMELINE_ITEM_KEYS:
        item = data.get(key)
        if isinstance(item, dict):
            summary["ref"] = item.get("ref")
            summary["subject"] = item.get("subject")
            break
    if changed_fields:
        summary["changed_fields"] = changed_fields
    status_diff = values_diff.get("status")
    if isinstance(status_diff, list) and len(status_diff) == 2:
        summary["status_change"] = {"from": status_diff[0], "to": status_diff[1]}
    if comment:
        if full_comments or len(comment) <= _ACTIVITY_COMMENT_CHARS:
            summary["comment"] = comment
        else:
            # Shortened to keep a 100-event feed compact, but visibly (#154:
            # the silent cut hid decisions); list_comments returns the full text.
            summary["comment"] = comment[:_ACTIVITY_COMMENT_CHARS]
            summary["comment_truncated"] = True
    return summary


def _parse_since(since: str) -> datetime:
    """An ISO date or datetime; a date, or a time without offset, is UTC."""
    try:
        moment = datetime.fromisoformat(since)
    except ValueError:
        raise ValueError(
            f"since must be an ISO date or datetime (e.g. '2026-11-02' or "
            f"'2026-11-02T09:00:00+01:00'), got {since!r}."
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _event_actor(event: Dict[str, Any]) -> Dict[str, Any]:
    return (event.get("data") or {}).get("user") or {}


def _actor_matcher(client, project_id: int, actors: List[str], session_id: str):
    """A predicate on raw events, plus the actor values that are not members.

    A member (email or full name) is matched on user id. Any other value (a
    former member, the GitHub integration user) is matched on the event's
    display name or username, case-insensitively.
    """
    ids, names = set(), {}
    for actor in actors:
        try:
            ids.add(resolve._resolve_user(client, project_id, actor, session_id))
        except ValueError:
            names[actor.strip().lower()] = actor

    def matches(event: Dict[str, Any]) -> bool:
        user = _event_actor(event)
        if user.get("id") in ids:
            return True
        return any(
            str(user.get(k) or "").lower() in names for k in ("name", "username") if user.get(k)
        )

    return matches, names


def _ticket_rows(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per item that moved, from event summaries (newest first)."""
    rows: Dict[Any, Dict[str, Any]] = {}
    for event in reversed(events):  # oldest first, so "from" is the first move
        key = (event["entity"], event.get("ref"))
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "entity": event["entity"],
                "ref": event.get("ref"),
                "subject": event.get("subject"),
                "events": 0,
                "comments": 0,
                "actors": [],
            }
        row["events"] += 1
        if event.get("subject"):
            row["subject"] = event["subject"]
        if event["actor"] not in row["actors"]:
            row["actors"].append(event["actor"])
        move = event.get("status_change")
        if move:
            row.setdefault("status_change", {"from": move["from"]})["to"] = move["to"]
        if event.get("comment"):
            # Already shortened by _summarize_timeline_event unless full_comments.
            row["comments"] += 1
            row["last_comment"] = event["comment"]
            if event.get("comment_truncated"):
                row["last_comment_truncated"] = True
            else:
                row.pop("last_comment_truncated", None)
        row["last_event"] = event["when"]
    return sorted(rows.values(), key=lambda r: r["last_event"] or "", reverse=True)


def _size(result: Dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str))


def _fit(result: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the result under _ACTIVITY_MAX_CHARS: events become ticket rows, then
    rows lose their last comment, then subjects are shortened, then the oldest
    rows go (too_large)."""
    if _size(result) <= _ACTIVITY_MAX_CHARS:
        return result
    if "events" in result:
        events = result.pop("events")
        result["group_by"] = "ticket"
        result["summarised"] = True
        result["tickets"] = _ticket_rows(events)
        result["ticket_count"] = len(result["tickets"])
        if _size(result) <= _ACTIVITY_MAX_CHARS:
            return result
    for row in result["tickets"]:
        row.pop("last_comment", None)
        row.pop("last_comment_truncated", None)
    result["comments_dropped"] = True
    if _size(result) > _ACTIVITY_MAX_CHARS:
        for row in result["tickets"]:
            subject = row.get("subject") or ""
            if len(subject) > _ACTIVITY_SHORT_SUBJECT:
                row["subject"] = subject[:_ACTIVITY_SHORT_SUBJECT]
        result["subjects_shortened"] = True
    total = len(result["tickets"])
    while result["tickets"] and _size(result) > _ACTIVITY_MAX_CHARS:
        cut = max(1, len(result["tickets"]) // 10)
        del result["tickets"][-cut:]
    if len(result["tickets"]) < total:
        result["too_large"] = True
        result["tickets_omitted"] = total - len(result["tickets"])
    return result


@app.tool(
    "get_project_activity",
    description=(
        "Return recent activity on a project — who did what to which item and when, newest "
        "first. Each event names its item (ref, subject) and, for a status move, "
        "status_change {from, to}. Answers 'what changed today/this week?'. "
        "Without since: the latest `limit` events (default 20, max 100). "
        "With since (ISO date or datetime; a date or a time without offset is UTC, e.g. "
        "'2026-11-02T00:00:00+01:00' for Paris midnight): EVERY event since then, read page "
        "by page, up to `limit` (default 500, max 2000); truncated=true means more events "
        "since that moment exist than were returned. "
        "actor: one member (email or full name) or a list; other names (e.g. 'GitHub') match "
        "the event's display name; unmatched_actors lists values that matched no event. "
        "entity: 'story', 'task', 'issue', 'epic', 'sprint', 'wiki', or a list of them. "
        "actor and entity filter on the server, and limit counts matching events only. "
        "group_by='ticket': one row per item instead of events — entity, ref, subject, "
        "status_change {from (first), to (last)} over the window, events, comments, actors, "
        "last_event, last_comment (200 characters). "
        "Size guard: a result over 50,000 characters is returned as ticket rows "
        "(summarised=true); rows that still don't fit lose last_comment "
        "(comments_dropped=true), then subjects are cut to 60 characters "
        "(subjects_shortened=true), then the oldest rows go (too_large=true, tickets_omitted). "
        "Comments longer than 200 characters are shortened and flagged "
        "comment_truncated=true unless full_comments=True; list_comments returns an "
        "item's comments in full. "
        "project accepts a slug (e.g. 'my-project') or numeric ID."
    ),
)
def get_project_activity(
    project: Union[str, int],
    limit: Optional[int] = None,
    full_comments: bool = False,
    since: Optional[str] = None,
    actor: Optional[Union[str, List[str]]] = None,
    entity: Optional[Union[str, List[str]]] = None,
    group_by: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    actual_session_id = core._get_session_id(session_id)
    client = core._get_authenticated_client(actual_session_id)

    if group_by not in (None, "ticket"):
        raise ValueError("group_by must be 'ticket' or omitted.")
    actors = [actor] if isinstance(actor, str) else list(actor or [])
    entities = [entity] if isinstance(entity, str) else list(entity or [])
    unknown = [e for e in entities if e not in _ACTIVITY_ENTITY_FILTER]
    if unknown:
        raise ValueError(
            f"Unknown entity {unknown}. Must be among: {', '.join(_ACTIVITY_ENTITY_FILTER)}."
        )
    prefixes = {_ACTIVITY_ENTITY_FILTER[e] for e in entities}
    since_moment = _parse_since(since) if since is not None else None
    max_limit = 2000 if since_moment else 100
    if limit is None:
        limit = 500 if since_moment else 20
    if limit < 1 or limit > max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}.")

    def do_activity():
        proj = resolve._resolve_project(client, project)
        project_id = proj["id"]
        result: Dict[str, Any] = {
            "project": {"id": proj["id"], "name": proj["name"], "slug": proj["slug"]},
        }
        by_actor, non_members = (lambda e: True), {}
        if actors:
            by_actor, non_members = _actor_matcher(client, project_id, actors, actual_session_id)

        def matches(event: Dict[str, Any]) -> bool:
            prefix = (event.get("event_type") or "").split(".")[0]
            return (not prefixes or prefix in prefixes) and by_actor(event)

        if since_moment is None:
            raw = client.api.get(
                f"/timeline/project/{project_id}",
                params={"page_size": limit},
            )
            events = [e for e in (raw if isinstance(raw, list) else []) if matches(e)]
        else:
            # The timeline is newest first: read pages until an event is older
            # than `since`, or one past the limit (to know whether it truncated).
            # A write landing mid-read shifts later pages down by one, so a page can
            # start with an event already taken: skip repeated ids.
            events, seen = [], set()
            page, done, scanned = 1, False, 0
            while not done:
                try:
                    raw = client.api.get(
                        f"/timeline/project/{project_id}",
                        params={"page_size": _ACTIVITY_PAGE_SIZE, "page": page},
                    )
                except TaigaNotFoundError:  # past the last page
                    break
                batch = raw if isinstance(raw, list) else []
                for event in batch:
                    if datetime.fromisoformat(event["created"]) < since_moment:
                        done = True
                        break
                    if event.get("id") in seen:
                        continue
                    seen.add(event.get("id"))
                    scanned += 1
                    if matches(event):
                        events.append(event)
                    if len(events) > limit or scanned >= _ACTIVITY_MAX_SCAN:
                        done = True
                        break
                if len(batch) < _ACTIVITY_PAGE_SIZE:
                    break
                page += 1
            result["since"] = since_moment.isoformat()
            result["truncated"] = len(events) > limit or scanned >= _ACTIVITY_MAX_SCAN
            events = events[:limit]

        if non_members:
            seen_names = {
                str(_event_actor(e).get(k) or "").lower()
                for e in events
                for k in ("name", "username")
            }
            unmatched = [v for k, v in non_members.items() if k not in seen_names]
            if unmatched:
                result["unmatched_actors"] = unmatched

        summaries = [_summarize_timeline_event(e, full_comments) for e in events]
        result["count"] = len(summaries)
        if group_by == "ticket":
            result["group_by"] = "ticket"
            result["tickets"] = _ticket_rows(summaries)
            result["ticket_count"] = len(result["tickets"])
        else:
            result["events"] = summaries
        return _fit(result)

    return core._execute_taiga_operation("get_project_activity", do_activity, str(project))
