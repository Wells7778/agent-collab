from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenthub import hubfs
from agenthub.schema import AgentStatus, Event, HubConfig, ProjectConfig
from conftest import REPO_ROOT, make_task


def test_atomic_write_creates_file_with_content_and_no_tmp_leftover(tmp_path: Path):
    target = tmp_path / "sub" / "file.txt"
    hubfs.atomic_write(target, "hello")
    assert target.read_text() == "hello"
    leftovers = list((tmp_path / "sub").glob(".*"))
    assert leftovers == []


def test_write_task_then_read_task_roundtrip(tmp_path: Path):
    task = make_task()
    path = tmp_path / "T-1.md"
    hubfs.write_task(path, task)
    reloaded = hubfs.read_task(path)
    assert reloaded == task


def test_move_task_removes_source_and_writes_dest(tmp_path: Path):
    task = make_task()
    src = tmp_path / "backlog" / f"{task.id}.md"
    hubfs.write_task(src, task)
    dest = tmp_path / "in-progress" / "claude" / f"{task.id}.md"
    hubfs.move_task(task, dest, src)
    assert not src.exists()
    assert hubfs.read_task(dest) == task


def test_move_task_to_the_same_path_keeps_the_file(tmp_path: Path):
    task = make_task()
    path = tmp_path / f"{task.id}.md"
    hubfs.write_task(path, task)
    hubfs.move_task(task, path, path)
    assert hubfs.read_task(path) == task


def test_move_task_rename_failure_never_creates_two_authoritative_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    task = make_task(id="T-1", status="review")
    src = tmp_path / "review" / "T-1.md"
    dest = tmp_path / "done" / "T-1.md"
    hubfs.write_task(src, task)
    updated = task.model_copy(update={"status": "done"})
    real_replace = hubfs.os.replace

    def fail_final_rename(old, new):
        if Path(old) == src and Path(new) == dest:
            raise OSError("simulated rename failure")
        return real_replace(old, new)

    monkeypatch.setattr(hubfs.os, "replace", fail_final_rename)

    with pytest.raises(OSError, match="simulated rename failure"):
        hubfs.move_task(updated, dest, src)

    assert src.is_file()
    assert not dest.exists()


def test_append_event_appends_multiple_valid_json_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    e1 = Event(ts=datetime.now(timezone.utc), actor="daemon", event="daemon_started")
    e2 = Event(ts=datetime.now(timezone.utc), actor="daemon", event="task_dispatched", task_id="T-1", agent="claude")
    hubfs.append_event(path, e1)
    hubfs.append_event(path, e2)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "daemon_started"
    assert json.loads(lines[1])["task_id"] == "T-1"


def test_write_status_then_read_status_roundtrip(tmp_path: Path):
    path = tmp_path / "claude.json"
    status = AgentStatus(agent="claude", state="idle", heartbeat_at=datetime.now(timezone.utc))
    hubfs.write_status(path, status)
    reloaded = hubfs.read_status(path)
    assert reloaded == status


def test_read_status_returns_none_when_missing(tmp_path: Path):
    assert hubfs.read_status(tmp_path / "missing.json") is None


def test_example_config_matches_protocol_shape():
    config = hubfs.load_config(REPO_ROOT / "config.example.yaml")
    assert isinstance(config, HubConfig)
    assert set(config.agents) == {"claude", "codex", "hermes", "agy"}
    assert config.max_concurrent_global == 2
    assert config.max_concurrent_per_branch_base == 2
    assert config.max_concurrent_per_agent == 1
    assert config.task_timeout_minutes == 120
    assert config.heartbeat_seconds == 60
    assert config.worktree_retention_days == 7
    assert config.max_generation == 3
    assert config.agents["codex"].probe == ["codex", "--version"]
    assert config.agents["codex"].command == [
        "caffeinate",
        "-i",
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
    ]


def test_example_config_keeps_the_fence_preserving_hermes_flag():
    config = hubfs.load_config(REPO_ROOT / "config.example.yaml")
    assert "-Q" in config.agents["hermes"].command


def test_example_projects_match_protocol_shape():
    projects = hubfs.load_projects(REPO_ROOT / "projects.example.yaml")
    assert isinstance(projects["my-service"], ProjectConfig)
    assert projects["my-service"].allowed_agents == ["claude", "codex"]
    assert projects["research-desk"].allowed_agents == ["agy"]
    assert projects["research-desk"].setup == []


def test_extract_pr_url_returns_the_last_pr_line():
    report = "### g0\n\nPR: https://example/pr/1\n\n### g1\n\nPR: https://example/pr/2\n"
    assert hubfs.extract_pr_url(report) == "https://example/pr/2"


def test_format_pr_line_round_trips_through_extract():
    assert hubfs.format_pr_line("https://x/pr/7") == "PR: https://x/pr/7"
    assert hubfs.extract_pr_url(hubfs.format_pr_line("https://x/pr/7")) == "https://x/pr/7"


def test_read_events_tail_drops_partial_first_line_when_chunk_truncates(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    base = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(50):
        hubfs.append_event(
            path,
            Event(ts=base + timedelta(seconds=i), actor="daemon", event="task_dispatched", task_id=f"T-{i}"),
        )
    events = hubfs.read_events_tail(path, limit=5, chunk_size=400)
    assert [e.task_id for e in events] == [f"T-{i}" for i in range(47, 50)]


def test_read_events_tail_limit_zero_returns_empty(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    hubfs.append_event(path, Event(ts=datetime.now(timezone.utc), actor="daemon", event="daemon_started"))
    assert hubfs.read_events_tail(path, 0) == []


def test_example_config_restricts_agy_to_research_and_others_to_code():
    config = hubfs.load_config(REPO_ROOT / "config.example.yaml")
    assert config.agents["agy"].task_types == ["research"]
    assert config.agents["agy"].skills == ["research"]
    for agent in ("claude", "codex", "hermes"):
        assert config.agents[agent].task_types == ["coding", "review"]


def test_example_config_anchors_rate_limit_cooldown_minutes():
    assert hubfs.load_config(REPO_ROOT / "config.example.yaml").rate_limit_cooldown_minutes == 30
