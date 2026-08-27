from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agenthub import hubfs
from agenthub.dashboard_api import create_app
from agenthub.scheduler import Scheduler
from agenthub.schema import AgentStatus, Event, WorkspaceInfo
from conftest import FakeClock, FakeRunner, make_task, put_task


@pytest.fixture
def client(hub_dir: Path) -> TestClient:
    return TestClient(create_app(hub_dir))


def dashboard_events(hub_dir: Path) -> list[Event]:
    return hubfs.read_events(hub_dir / "events" / "dashboard.jsonl")


def daemon_events(hub_dir: Path) -> list[Event]:
    return hubfs.read_events(hub_dir / "events" / "events.jsonl")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def test_list_tasks_empty_board(client: TestClient):
    res = client.get("/api/tasks")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "backlog": [],
        "in_progress": [],
        "blocked": [],
        "review": [],
        "done": [],
        "invalid_count": 0,
    }


def test_list_tasks_returns_summaries_across_sections_and_filters_by_project(
    client: TestClient, hub_dir: Path
):
    put_task(hub_dir, "backlog", make_task(id="T-1", project="proj-a"))
    put_task(
        hub_dir,
        "in-progress/claude",
        make_task(id="T-2", project="proj-b", status="in-progress", claimed_by="claude"),
    )
    put_task(hub_dir, "blocked", make_task(id="T-3", project="proj-a", status="blocked"))
    put_task(hub_dir, "review", make_task(id="T-4", project="proj-b", status="review"))
    put_task(hub_dir, "done", make_task(id="T-5", project="proj-a", status="done"))

    res = client.get("/api/tasks")
    assert res.status_code == 200
    body = res.json()
    assert [t["id"] for t in body["backlog"]] == ["T-1"]
    assert [t["id"] for t in body["in_progress"]] == ["T-2"]
    assert [t["id"] for t in body["blocked"]] == ["T-3"]
    assert [t["id"] for t in body["review"]] == ["T-4"]
    assert [t["id"] for t in body["done"]] == ["T-5"]
    assert body["in_progress"][0]["claimed_by"] == "claude"

    filtered = client.get("/api/tasks", params={"project": "proj-a"}).json()
    assert sorted(t["id"] for section in filtered.values() if isinstance(section, list) for t in section) == [
        "T-1",
        "T-3",
        "T-5",
    ]


def test_list_tasks_skips_invalid_files_and_reports_invalid_count(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-1"))
    (hub_dir / "tasks" / "backlog" / "T-broken.md").write_text("this is not a valid task file")

    res = client.get("/api/tasks")
    body = res.json()
    assert [t["id"] for t in body["backlog"]] == ["T-1"]
    assert body["invalid_count"] == 1


def test_get_task_returns_full_content(client: TestClient, hub_dir: Path):
    task = make_task(id="T-1", requirement_md="do X", acceptance_md="- [ ] X done", report_md="progress")
    put_task(hub_dir, "backlog", task)

    res = client.get("/api/tasks/T-1")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "T-1"
    assert body["requirement_md"] == "do X"
    assert body["acceptance_md"] == "- [ ] X done"
    assert body["report_md"] == "progress"


def test_get_task_404_when_missing(client: TestClient):
    res = client.get("/api/tasks/T-missing")
    assert res.status_code == 404


@pytest.mark.parametrize("subdir,status", [
    ("in-progress/claude", "in-progress"), ("done", "done"), ("invalid", "backlog"),
])
def test_get_task_finds_tasks_in_every_directory(client: TestClient, hub_dir: Path, subdir, status):
    put_task(hub_dir, subdir, make_task(id="T-9", status=status))
    assert client.get("/api/tasks/T-9").status_code == 200


def test_complete_on_in_progress_task_is_409_not_404(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "in-progress/claude", make_task(id="T-9", status="in-progress", claimed_by="claude"))
    assert client.post("/api/tasks/T-9/complete").status_code == 409


def test_list_status_returns_written_statuses(client: TestClient, hub_dir: Path):
    hubfs.write_status(
        hub_dir / "status" / "claude.json",
        AgentStatus(agent="claude", state="working", task_id="T-1", project="proj-a",
                    phase="running", pid=1, pgid=1, started_at=datetime.now(timezone.utc),
                    heartbeat_at=datetime.now(timezone.utc)),
    )
    hubfs.write_status(
        hub_dir / "status" / "codex.json",
        AgentStatus(agent="codex", state="idle", heartbeat_at=datetime.now(timezone.utc)),
    )

    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    assert {s["agent"] for s in body} == {"claude", "codex"}
    claude_status = next(s for s in body if s["agent"] == "claude")
    assert claude_status["state"] == "working"
    assert claude_status["task_id"] == "T-1"


def test_list_events_merges_sorts_and_limits(client: TestClient, hub_dir: Path):
    base = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)
    hubfs.append_event(
        hub_dir / "events" / "events.jsonl",
        Event(ts=base, actor="daemon", event="daemon_started"),
    )
    hubfs.append_event(
        hub_dir / "events" / "events.jsonl",
        Event(ts=base + timedelta(seconds=2), actor="daemon", event="task_dispatched", task_id="T-1"),
    )
    hubfs.append_event(
        hub_dir / "events" / "dashboard.jsonl",
        Event(ts=base + timedelta(seconds=1), actor="dashboard", event="task_created", task_id="T-2"),
    )

    res = client.get("/api/events", params={"limit": 100})
    events = res.json()
    assert [e["event"] for e in events] == ["daemon_started", "task_created", "task_dispatched"]

    limited = client.get("/api/events", params={"limit": 2}).json()
    assert [e["event"] for e in limited] == ["task_created", "task_dispatched"]


def test_list_events_tail_read_returns_correct_last_events_beyond_limit(client: TestClient, hub_dir: Path):
    base = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)
    events_path = hub_dir / "events" / "events.jsonl"
    for i in range(30):
        hubfs.append_event(
            events_path,
            Event(ts=base + timedelta(seconds=i), actor="daemon", event="task_dispatched", task_id=f"T-{i}"),
        )

    res = client.get("/api/events", params={"limit": 5})
    events = res.json()
    assert [e["task_id"] for e in events] == [f"T-{i}" for i in range(25, 30)]


def test_list_messages_returns_files_and_content(client: TestClient, hub_dir: Path):
    (hub_dir / "messages" / "2026-08-26T10-00-00-claude-human.md").write_text("提問內容")

    res = client.get("/api/messages")
    body = res.json()
    assert len(body) == 1
    assert body[0]["filename"] == "2026-08-26T10-00-00-claude-human.md"
    assert body[0]["content"] == "提問內容"


def test_list_messages_limit_returns_only_newest_files(client: TestClient, hub_dir: Path):
    messages_dir = hub_dir / "messages"
    for i in range(7):
        (messages_dir / f"2026-08-26T10-00-{i:02d}-claude-human.md").write_text(f"msg {i}")

    res = client.get("/api/messages", params={"limit": 3})
    body = res.json()
    assert [b["filename"] for b in body] == [
        f"2026-08-26T10-00-{i:02d}-claude-human.md" for i in (4, 5, 6)
    ]


def test_list_projects_returns_keys(client: TestClient):
    res = client.get("/api/projects")
    assert res.status_code == 200
    assert res.json() == {"projects": ["proj-a", "proj-b", "proj-c"]}


def test_create_task_success_writes_backlog_and_event(client: TestClient, hub_dir: Path):
    payload = {
        "title": "Fix the thing",
        "project": "proj-a",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": ["general"],
        "priority": "P1",
        "asana_url": "https://app.asana.com/0/1/2",
    }
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 201
    body = res.json()
    assert body["id"].startswith(f"T-{today_str()}-")
    assert body["status"] == "backlog"
    assert body["source"]["type"] == "asana"
    assert body["source"]["asana_url"] == payload["asana_url"]

    dest = hub_dir / "tasks" / "backlog" / f"{body['id']}.md"
    assert dest.is_file()
    stored = hubfs.read_task(dest)
    assert stored.title == "Fix the thing"

    events = dashboard_events(hub_dir)
    assert any(e.event == "task_created" and e.task_id == body["id"] for e in events)


def test_create_task_without_asana_url_is_manual_source(client: TestClient):
    payload = {
        "title": "T",
        "project": "proj-a",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": [],
        "priority": "P2",
    }
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 201
    assert res.json()["source"]["type"] == "manual"


def test_create_task_id_uniqueness_scans_all_task_directories(client: TestClient, hub_dir: Path):
    today = today_str()
    put_task(hub_dir, "backlog", make_task(id=f"T-{today}-001"))
    put_task(hub_dir, "in-progress/claude", make_task(id=f"T-{today}-003", status="in-progress"))
    put_task(hub_dir, "review", make_task(id=f"T-{today}-002", status="review"))
    put_task(hub_dir, "done", make_task(id=f"T-{today}-005", status="done"))

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    put_task(hub_dir, "done", make_task(id=f"T-{yesterday}-099", status="done"))

    payload = {
        "title": "New",
        "project": "proj-a",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": [],
        "priority": "P2",
    }
    res = client.post("/api/tasks", json=payload)
    assert res.json()["id"] == f"T-{today}-006"


def test_concurrent_create_task_requests_never_reuse_an_id(
    client: TestClient, hub_dir: Path
):
    def create(index: int):
        return client.post(
            "/api/tasks",
            json={
                "title": f"Concurrent task {index}",
                "project": "proj-a",
                "requirement_md": "Do the thing.",
                "acceptance_md": "- [ ] it works",
                "skills_required": ["general"],
                "priority": "P2",
                "asana_url": None,
            },
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(create, range(32)))

    assert all(response.status_code == 201 for response in responses)
    ids = [response.json()["id"] for response in responses]
    assert len(set(ids)) == 32
    saved = [
        hubfs.read_task(path)
        for path in hubfs.list_task_files(hub_dir / "tasks" / "backlog")
    ]
    assert {task.title for task in saved} == {
        f"Concurrent task {index}" for index in range(32)
    }


def test_create_task_unknown_project_returns_400(client: TestClient):
    payload = {
        "title": "T",
        "project": "proj-does-not-exist",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": [],
        "priority": "P2",
    }
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 400


def test_create_task_illegal_priority_returns_422(client: TestClient):
    payload = {
        "title": "T",
        "project": "proj-a",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": [],
        "priority": "P9",
    }
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 422


@pytest.mark.parametrize("field", ["title", "requirement_md", "acceptance_md"])
def test_create_task_rejects_blank_required_text(client: TestClient, field: str):
    payload = {
        "title": "T",
        "project": "proj-a",
        "requirement_md": "req",
        "acceptance_md": "acc",
        "skills_required": [],
        "priority": "P2",
    }
    payload[field] = "   "

    assert client.post("/api/tasks", json=payload).status_code == 422


def test_reply_success_moves_to_backlog_and_clears_claim(client: TestClient, hub_dir: Path):
    task = make_task(id="T-1", status="blocked", claimed_by="claude", claimed_at=datetime.now(timezone.utc))
    put_task(hub_dir, "blocked", task)

    res = client.post("/api/tasks/T-1/reply", json={"reply_md": "answer here"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "backlog"
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None
    assert "answer here" in body["report_md"]
    assert "人類回覆" in body["report_md"]

    assert not (hub_dir / "tasks" / "blocked" / "T-1.md").exists()
    assert (hub_dir / "tasks" / "backlog" / "T-1.md").is_file()

    events = dashboard_events(hub_dir)
    assert any(e.event == "task_replied" and e.task_id == "T-1" and e.agent == "claude" for e in events)


def test_reply_rejects_blank_text(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "blocked", make_task(id="T-1", status="blocked"))

    res = client.post("/api/tasks/T-1/reply", json={"reply_md": "   "})

    assert res.status_code == 422
    assert (hub_dir / "tasks" / "blocked" / "T-1.md").is_file()


def test_reply_fails_for_task_not_in_blocked(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review"))
    res = client.post("/api/tasks/T-1/reply", json={"reply_md": "x"})
    assert res.status_code == 409


def test_reply_fails_for_missing_task(client: TestClient):
    res = client.post("/api/tasks/T-missing/reply", json={"reply_md": "x"})
    assert res.status_code == 404


def test_complete_success_moves_to_done(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review", claimed_by="claude"))

    res = client.post("/api/tasks/T-1/complete")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert not (hub_dir / "tasks" / "review" / "T-1.md").exists()
    assert (hub_dir / "tasks" / "done" / "T-1.md").is_file()

    events = dashboard_events(hub_dir)
    assert any(e.event == "task_completed" and e.task_id == "T-1" for e in events)


def test_complete_fails_for_task_not_in_review(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-1"))
    res = client.post("/api/tasks/T-1/complete")
    assert res.status_code == 409


def test_return_with_explicit_assigned_to(client: TestClient, hub_dir: Path):
    put_task(
        hub_dir,
        "review",
        make_task(id="T-1", status="review", claimed_by="claude", claimed_at=datetime.now(timezone.utc)),
    )

    res = client.post("/api/tasks/T-1/return", json={"feedback_md": "fix this", "assigned_to": "codex"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "backlog"
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None
    assert body["assigned_to"] == "codex"
    assert "打回意見" in body["report_md"]
    assert "fix this" in body["report_md"]
    assert (hub_dir / "tasks" / "backlog" / "T-1.md").is_file()


def test_return_rejects_blank_feedback(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review"))

    res = client.post("/api/tasks/T-1/return", json={"feedback_md": "   "})

    assert res.status_code == 422
    assert (hub_dir / "tasks" / "review" / "T-1.md").is_file()


def test_return_without_assigned_to_defaults_to_previous_agent(client: TestClient, hub_dir: Path):
    put_task(
        hub_dir,
        "review",
        make_task(id="T-1", status="review", claimed_by="claude", claimed_at=datetime.now(timezone.utc)),
    )

    res = client.post("/api/tasks/T-1/return", json={"feedback_md": "fix this"})
    assert res.status_code == 200
    body = res.json()
    assert body["assigned_to"] == "claude"
    assert body["claimed_at"] is None


def test_return_fails_for_task_not_in_review(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "blocked", make_task(id="T-1", status="blocked"))
    res = client.post("/api/tasks/T-1/return", json={"feedback_md": "x"})
    assert res.status_code == 409


def test_cancel_success_from_review(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review"))
    res = client.post("/api/tasks/T-1/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    assert (hub_dir / "tasks" / "done" / "T-1.md").is_file()


def test_cancel_success_from_blocked(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "blocked", make_task(id="T-1", status="blocked"))
    res = client.post("/api/tasks/T-1/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    assert (hub_dir / "tasks" / "done" / "T-1.md").is_file()


def test_cancel_fails_for_task_not_in_review_or_blocked(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-1"))
    res = client.post("/api/tasks/T-1/cancel")
    assert res.status_code == 409


def test_cancel_and_return_only_write_dashboard_events(client: TestClient, hub_dir: Path):
    daemon_events_path = hub_dir / "events" / "events.jsonl"
    hubfs.append_event(
        daemon_events_path, Event(ts=datetime.now(timezone.utc), actor="daemon", event="daemon_started")
    )
    before = daemon_events(hub_dir)

    put_task(hub_dir, "review", make_task(id="T-1", status="review", claimed_by="claude"))
    put_task(hub_dir, "blocked", make_task(id="T-2", status="blocked"))
    put_task(hub_dir, "review", make_task(id="T-3", status="review", claimed_by="claude"))

    client.post("/api/tasks/T-1/cancel")
    client.post("/api/tasks/T-2/cancel")
    client.post("/api/tasks/T-3/return", json={"feedback_md": "redo"})

    assert daemon_events(hub_dir) == before
    assert [e.actor for e in dashboard_events(hub_dir)] == ["dashboard"] * 3


@pytest.mark.parametrize("src_dir,status", [("review", "review"), ("blocked", "blocked")])
def test_cancel_emits_task_cancelled_with_source_dir(client: TestClient, hub_dir: Path, src_dir, status):
    put_task(hub_dir, src_dir, make_task(id="T-1", status=status, claimed_by="claude"))
    client.post("/api/tasks/T-1/cancel")
    events = [e for e in dashboard_events(hub_dir) if e.task_id == "T-1"]
    assert [e.event for e in events] == ["task_cancelled"]
    assert events[0].agent == "claude"
    assert events[0].detail["from"] == src_dir


def test_return_emits_task_returned_with_previous_agent(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review", claimed_by="claude"))
    client.post("/api/tasks/T-1/return", json={"feedback_md": "redo", "assigned_to": "codex"})
    events = [e for e in dashboard_events(hub_dir) if e.task_id == "T-1"]
    assert [e.event for e in events] == ["task_returned"]
    assert events[0].agent == "codex"
    assert events[0].detail["previous_agent"] == "claude"


def test_dispatch_review_creates_related_task_with_pr_line(client: TestClient, hub_dir: Path):
    original = make_task(
        id="T-1",
        status="review",
        project="proj-a",
        skills_required=["general"],
        priority="P1",
        workspace=WorkspaceInfo(
            repo="codecommit::us-west-2://proj-a",
            branch_base="develop",
            branch="agent/claude/T-1-g0",
        ),
        report_md="### Final (completed) ...\n\nsummary\n\nPR: https://example.com/pr/1",
    )
    put_task(hub_dir, "review", original)

    res = client.post("/api/tasks/T-1/review", json={"assigned_to": "codex"})
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "review"
    assert body["related_task"] == "T-1"
    assert body["assigned_to"] == "codex"
    assert body["project"] == "proj-a"
    assert body["skills_required"] == ["general"]
    assert body["priority"] == "P1"
    assert "https://example.com/pr/1" in body["requirement_md"]
    assert body["status"] == "backlog"

    assert (hub_dir / "tasks" / "backlog" / f"{body['id']}.md").is_file()
    assert (hub_dir / "tasks" / "review" / "T-1.md").is_file()

    events = dashboard_events(hub_dir)
    assert any(
        e.event == "review_task_created" and e.task_id == body["id"] and e.detail.get("related_task") == "T-1"
        for e in events
    )


def test_dispatch_review_rejects_original_implementer(client: TestClient, hub_dir: Path):
    put_task(
        hub_dir,
        "review",
        make_task(id="T-1", status="review", claimed_by="claude"),
    )

    res = client.post("/api/tasks/T-1/review", json={"assigned_to": "claude"})

    assert res.status_code == 409
    assert res.json()["detail"] == "review must be assigned to a different agent than claude"
    assert hubfs.list_task_files(hub_dir / "tasks" / "backlog") == []


def test_dispatch_review_auto_assigns_different_qualified_agent(
    client: TestClient, hub_dir: Path
):
    put_task(
        hub_dir,
        "review",
        make_task(
            id="T-1",
            status="review",
            claimed_by="claude",
            project="proj-a",
            skills_required=["general"],
        ),
    )

    res = client.post("/api/tasks/T-1/review", json={})

    assert res.status_code == 200
    assert res.json()["assigned_to"] == "codex"


@pytest.mark.parametrize("assigned_to", ["", "   "])
def test_dispatch_review_rejects_blank_assignee_before_scheduler_dispatch(
    client: TestClient, hub_dir: Path, assigned_to: str
):
    put_task(
        hub_dir,
        "review",
        make_task(id="T-1", status="review", claimed_by="claude"),
    )

    res = client.post("/api/tasks/T-1/review", json={"assigned_to": assigned_to})
    clock = FakeClock()
    runner = FakeRunner(clock)
    Scheduler.from_hub_dir(hub_dir, runner, clock).tick()

    assert res.status_code == 422
    assert runner.spawn_calls == []
    assert hubfs.list_task_files(hub_dir / "tasks" / "backlog") == []


def test_dispatch_review_falls_back_to_branch_when_no_pr_line(client: TestClient, hub_dir: Path):
    original = make_task(
        id="T-1",
        status="review",
        workspace=WorkspaceInfo(
            repo="codecommit::us-west-2://proj-a",
            branch_base="develop",
            branch="agent/claude/T-1-g0",
        ),
        report_md="no pr link here",
    )
    put_task(hub_dir, "review", original)

    res = client.post("/api/tasks/T-1/review", json={})
    assert res.status_code == 200
    body = res.json()
    assert "agent/claude/T-1-g0" in body["requirement_md"]
    assert body["assigned_to"] is None


def test_dispatch_review_without_body_uses_no_assigned_to(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-1", status="review"))
    res = client.post("/api/tasks/T-1/review")
    assert res.status_code == 200
    assert res.json()["assigned_to"] is None


def test_dispatch_review_fails_for_task_not_in_review(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-1"))
    res = client.post("/api/tasks/T-1/review", json={})
    assert res.status_code == 409


def test_review_task_carries_protocol_review_constraints(client: TestClient, hub_dir: Path):
    put_task(
        hub_dir,
        "review",
        make_task(
            id="T-1",
            status="review",
            workspace=WorkspaceInfo(repo="r", branch_base="develop", branch="agent/claude/T-1-g0"),
            report_md="PR: https://example.com/pr/1",
        ),
    )
    body = client.post("/api/tasks/T-1/review", json={}).json()
    assert body["acceptance_md"] == "- [ ] 審查報告已寫回本任務執行報告(不開 PR、不留 PR comment)"
    assert body["requirement_md"] == (
        "互審任務:審查 T-1 的變更。\n\n分支:agent/claude/T-1-g0\nPR:https://example.com/pr/1"
    )


def test_review_requirement_marks_absent_pr_and_unknown_branch(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "review", make_task(id="T-2", status="review", report_md="no link"))
    body = client.post("/api/tasks/T-2/review", json={}).json()
    assert "分支:(未知分支)" in body["requirement_md"]
    assert "PR:(無 PR,見分支)" in body["requirement_md"]


def test_dashboard_actions_never_touch_daemon_events_or_status_or_in_progress(
    client: TestClient, hub_dir: Path
):
    daemon_events_path = hub_dir / "events" / "events.jsonl"
    hubfs.append_event(daemon_events_path, Event(ts=datetime.now(timezone.utc), actor="daemon", event="daemon_started"))
    daemon_events_before = daemon_events(hub_dir)

    status_dir = hub_dir / "status"
    status_files_before = sorted(p.name for p in status_dir.glob("*"))

    in_progress_root = hub_dir / "tasks" / "in-progress"
    in_progress_files_before = sorted(str(p) for p in in_progress_root.rglob("*.md"))

    put_task(hub_dir, "blocked", make_task(id="T-1", status="blocked"))
    put_task(hub_dir, "review", make_task(id="T-2", status="review", claimed_by="claude"))
    put_task(hub_dir, "review", make_task(id="T-3", status="review"))

    client.post("/api/tasks", json={
        "title": "T", "project": "proj-a", "requirement_md": "r", "acceptance_md": "a",
        "skills_required": [], "priority": "P2",
    })
    client.post("/api/tasks/T-1/reply", json={"reply_md": "ans"})
    client.post("/api/tasks/T-2/complete")
    client.post("/api/tasks/T-3/review", json={})

    assert daemon_events(hub_dir) == daemon_events_before
    assert sorted(p.name for p in status_dir.glob("*")) == status_files_before
    assert sorted(str(p) for p in in_progress_root.rglob("*.md")) == in_progress_files_before
    assert len(dashboard_events(hub_dir)) >= 4


def test_index_serves_dashboard_html(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "task-card" in response.text


def test_dashboard_working_agent_ui_shows_project_and_excludes_implementer_from_review(
    client: TestClient,
):
    html = client.get("/").text

    assert (
        'detail = [r.task.id || "", r.task.project || "", r.task.title || ""]'
        '.filter(Boolean).join(" · ");'
    ) in html
    assert "if (n === excludedAgent) continue;" in html
    assert "agentOptions(null, task.claimed_by)" in html


def test_session_log_parses_stream_output(client: TestClient, hub_dir: Path, tmp_path: Path):
    import yaml

    from conftest import DEFAULT_CONFIG

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["workspaces_root"] = str(tmp_path / "ws")
    (hub_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    put_task(hub_dir, "in-progress/claude", make_task(id="T-20260826-950", claimed_by="claude", status="in-progress"))
    run_dir = tmp_path / "ws" / "proj-a" / "claude" / "T-20260826-950.hub"
    run_dir.mkdir(parents=True)
    (run_dir / "stdout-g0.log").write_text(
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"分析中"},{"type":"tool_use","name":"Bash","input":{"command":"cargo test"}}]}}\n'
        'not json noise\n'
        '{"type":"result","result":"完成"}\n'
    )

    body = client.get("/api/sessions/T-20260826-950/log").json()

    assert [e["kind"] for e in body["entries"]] == ["sys", "say", "tool", "result"]
    assert body["entries"][1]["text"] == "分析中"
    assert "cargo test" in body["entries"][2]["text"]
    assert body["log_bytes"] > 0


def test_session_log_streams_codex_jsonl_output(client: TestClient, hub_dir: Path, tmp_path: Path):
    import yaml

    from conftest import DEFAULT_CONFIG

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["workspaces_root"] = str(tmp_path / "ws")
    (hub_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    put_task(
        hub_dir,
        "in-progress/codex",
        make_task(
            id="T-20260826-952",
            claimed_by="codex",
            status="in-progress",
        ),
    )
    run_dir = tmp_path / "ws" / "proj-a" / "codex" / "T-20260826-952.hub"
    run_dir.mkdir(parents=True)
    log_path = run_dir / "stdout-g0.log"
    log_path.write_text(
        '{"type":"thread.started","thread_id":"thread-1"}\n'
        '{"type":"item.completed","item":{"id":"item-1","type":"agent_message","text":"分析中"}}\n'
    )

    first = client.get("/api/sessions/T-20260826-952/log").json()

    assert first["entries"] == [
        {"kind": "sys", "text": "Codex session 啟動"},
        {"kind": "say", "text": "分析中"},
    ]

    with log_path.open("a") as log:
        log.write(
            '{"type":"item.started","item":{"id":"item-2","type":"command_execution","command":"pytest -q","status":"in_progress"}}\n'
            '{"type":"item.completed","item":{"id":"item-2","type":"command_execution","command":"pytest -q","aggregated_output":"2 passed","exit_code":0,"status":"completed"}}\n'
            '{"type":"item.started","item":{"id":"item-3","type":"mcp_tool_call","server":"codebase-memory-mcp","tool":"search_graph","arguments":{"query":"live output"},"status":"in_progress"}}\n'
            '{"type":"item.completed","item":{"id":"item-3","type":"mcp_tool_call","server":"codebase-memory-mcp","tool":"search_graph","result":{},"status":"completed"}}\n'
            '{"type":"item.started","item":{"id":"item-4","type":"web_search","query":"Codex JSONL"}}\n'
            '{"type":"item.completed","item":{"id":"item-4","type":"web_search","query":"Codex JSONL"}}\n'
            '{"type":"item.completed","item":{"id":"item-5","type":"error","message":"request failed"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}\n'
        )

    completed = client.get("/api/sessions/T-20260826-952/log").json()

    assert [entry["kind"] for entry in completed["entries"]] == [
        "sys",
        "say",
        "tool",
        "result",
        "tool",
        "result",
        "tool",
        "result",
        "result",
        "result",
    ]
    assert completed["entries"][2]["text"] == "pytest -q"
    assert completed["entries"][3]["text"] == "pytest -q\n2 passed"
    assert completed["entries"][4]["text"] == "codebase-memory-mcp.search_graph"
    assert completed["entries"][5]["text"] == "codebase-memory-mcp.search_graph 完成"
    assert completed["entries"][6]["text"] == "web_search Codex JSONL"
    assert completed["entries"][7]["text"] == "web_search Codex JSONL 完成"
    assert completed["entries"][8]["text"] == "Codex 錯誤: request failed"
    assert completed["entries"][9]["text"] == "Codex turn 完成"


def test_session_log_without_logfile_returns_empty(client: TestClient, hub_dir: Path):
    put_task(hub_dir, "in-progress/claude", make_task(id="T-20260826-951", claimed_by="claude", status="in-progress"))
    body = client.get("/api/sessions/T-20260826-951/log").json()
    assert body["entries"] == []


def test_session_log_unknown_task_is_404(client: TestClient):
    assert client.get("/api/sessions/T-20260826-999/log").status_code == 404
