from __future__ import annotations

from pathlib import Path

import pytest

from agenthub import hubfs
from agenthub.schema import WorkspaceInfo
from agenthub.scheduler import Scheduler
from conftest import FakeClock, FakeRunner, make_task, put_task, read_events, write_config


def test_empty_backlog_noop(scheduler: Scheduler, hub_dir: Path):
    scheduler.tick()
    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []


def test_valid_dispatch_moves_task_and_writes_status_and_events(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task = make_task(id="T-20260826-001", skills_required=["general"], project="proj-a")
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    src = hub_dir / "tasks" / "backlog" / "T-20260826-001.md"
    dest = hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-001.md"
    assert not src.exists()
    assert dest.exists()

    dispatched = hubfs.read_task(dest)
    assert dispatched.workspace.branch == "agent/claude/T-20260826-001-g0"
    assert dispatched.workspace.repo == "codecommit::us-west-2://proj-a"
    assert dispatched.workspace.branch_base == "develop"
    assert dispatched.claimed_by == "claude"
    assert dispatched.claimed_at is not None
    assert dispatched.status == "in-progress"

    events = read_events(hub_dir)
    assert any(e.event == "task_dispatched" and e.task_id == "T-20260826-001" for e in events)
    assert any(e.event == "agent_spawned" and e.task_id == "T-20260826-001" for e in events)

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.state == "working"
    assert status.task_id == "T-20260826-001"
    assert status.pid is not None

    assert len(runner.spawn_calls) == 1


def test_status_records_pid_and_pgid_distinctly(scheduler: Scheduler, hub_dir: Path):
    task = make_task(id="T-20260826-002", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.pgid == status.pid + 5000


def test_invalid_task_moved_to_invalid_dir(scheduler: Scheduler, hub_dir: Path):
    bad_path = hub_dir / "tasks" / "backlog" / "T-broken.md"
    bad_path.write_text("this is not a valid task file at all\n")

    scheduler.tick()

    assert not bad_path.exists()
    assert (hub_dir / "tasks" / "invalid" / "T-broken.md").exists()

    events = read_events(hub_dir)
    assert any(e.event == "task_invalid" for e in events)


def test_task_invalid_event_uses_frontmatter_id_when_available(scheduler: Scheduler, hub_dir: Path):
    (hub_dir / "tasks" / "backlog" / "weird-filename.md").write_text(
        "---\nid: T-20260826-370\ntype: coding\ntitle: x\nsource:\n  type: manual\n"
        "project: proj-a\npriority: P2\n---\n\n## 需求描述\n\na\n"
    )
    scheduler.tick()

    invalid_events = [e for e in read_events(hub_dir) if e.event == "task_invalid"]
    assert [e.task_id for e in invalid_events] == ["T-20260826-370"]
    assert invalid_events[0].detail["file"] == "weird-filename.md"
    assert (hub_dir / "tasks" / "invalid" / "weird-filename.md").exists()


def test_schema_invalid_task_quarantined_without_killing_the_tick(scheduler: Scheduler, hub_dir: Path):
    (hub_dir / "tasks" / "backlog" / "T-20260826-900.md").write_text(
        "---\nid: T-20260826-900\ntype: coding\ntitle: x\nsource:\n  type: manual\n"
        "project: proj-a\npriority: P9\n---\n\n## 需求描述\n\na\n\n## 驗收標準\n\nb\n\n## 執行報告\n\n\n"
    )
    healthy = make_task(id="T-20260826-901", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", healthy)

    scheduler.tick()

    assert (hub_dir / "tasks" / "invalid" / "T-20260826-900.md").exists()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-901.md").exists()
    invalid_events = [e for e in read_events(hub_dir) if e.event == "task_invalid"]
    assert [e.task_id for e in invalid_events] == ["T-20260826-900"]


def test_schema_invalid_in_progress_file_does_not_kill_the_tick(scheduler: Scheduler, hub_dir: Path):
    (hub_dir / "tasks" / "in-progress" / "claude" / "corrupt.md").write_text(
        "---\nid: T-CORRUPT\ntype: coding\ntitle: x\nsource:\n  type: manual\n"
        "project: proj-a\npriority: P9\n---\n\n## 需求描述\n\na\n\n## 驗收標準\n\nb\n\n## 執行報告\n\n\n"
    )
    healthy = make_task(id="T-20260826-902", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", healthy)
    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-902.md").exists()


def test_depends_on_blocks_until_dependency_done_and_not_cancelled(scheduler: Scheduler, hub_dir: Path):
    dependent = make_task(id="T-20260826-010", depends_on=["T-20260826-999"], project="proj-b")
    put_task(hub_dir, "backlog", dependent)

    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / "T-20260826-010.md").exists()

    dep = make_task(id="T-20260826-999", project="proj-b", status="done")
    put_task(hub_dir, "done", dep)

    scheduler.tick()
    assert not (hub_dir / "tasks" / "backlog" / "T-20260826-010.md").exists()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-010.md").exists()


def test_depends_on_cancelled_dependency_never_dispatches(scheduler: Scheduler, hub_dir: Path):
    dependent = make_task(id="T-20260826-011", depends_on=["T-20260826-998"], project="proj-b")
    put_task(hub_dir, "backlog", dependent)
    dep = make_task(id="T-20260826-998", project="proj-b", status="cancelled")
    put_task(hub_dir, "done", dep)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-011.md").exists()


def test_all_dependencies_must_be_done_not_just_the_first(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "done", make_task(id="T-20260826-350", project="proj-b", status="done"))
    dependent = make_task(
        id="T-20260826-352", project="proj-b", depends_on=["T-20260826-350", "T-20260826-351"]
    )
    put_task(hub_dir, "backlog", dependent)

    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / "T-20260826-352.md").exists()

    put_task(hub_dir, "done", make_task(id="T-20260826-351", project="proj-b", status="done"))
    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-352.md").exists()


def test_last_of_multiple_dependencies_cancelled_never_dispatches(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "done", make_task(id="T-20260826-353", project="proj-b", status="done"))
    put_task(hub_dir, "done", make_task(id="T-20260826-354", project="proj-b", status="cancelled"))
    dependent = make_task(
        id="T-20260826-355", project="proj-b", depends_on=["T-20260826-353", "T-20260826-354"]
    )
    put_task(hub_dir, "backlog", dependent)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-355.md").exists()


def test_unreadable_dependency_file_blocks_dispatch_without_crashing(scheduler: Scheduler, hub_dir: Path):
    (hub_dir / "tasks" / "done" / "T-20260826-410.md").write_text("garbage, no frontmatter\n")
    dependent = make_task(id="T-20260826-411", project="proj-b", depends_on=["T-20260826-410"])
    put_task(hub_dir, "backlog", dependent)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-411.md").exists()


def test_global_cap_blocks_third_task(scheduler: Scheduler, hub_dir: Path):
    t1 = make_task(id="T-20260826-020", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", t1)
    t2 = make_task(id="T-20260826-021", project="proj-b", claimed_by="codex", status="in-progress")
    put_task(hub_dir, "in-progress/codex", t2)

    t3 = make_task(id="T-20260826-022", project="proj-c", skills_required=["general"])
    put_task(hub_dir, "backlog", t3)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-022.md").exists()


def test_two_coding_tasks_same_project_run_in_parallel_when_branch_base_cap_allows(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    write_config(hub_dir, max_concurrent_global=2, max_concurrent_per_branch_base=2)
    scheduler = Scheduler.from_hub_dir(hub_dir, runner, clock)

    put_task(hub_dir, "backlog", make_task(id="T-20260826-060", project="proj-a"))
    put_task(hub_dir, "backlog", make_task(id="T-20260826-061", project="proj-a"))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-060.md").exists()
    assert (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-061.md").exists()
    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []


def test_review_task_does_not_consume_the_branch_base_write_slot(scheduler: Scheduler, hub_dir: Path):
    coding = make_task(id="T-20260826-062", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", coding)
    put_task(hub_dir, "backlog", make_task(id="T-20260826-063", project="proj-a", type="review"))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-063.md").exists()


def test_stale_branch_base_on_a_requeued_task_cannot_bypass_the_write_slot_cap(
    scheduler: Scheduler, hub_dir: Path
):
    running = make_task(id="T-20260826-064", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", running)
    requeued = make_task(
        id="T-20260826-065",
        project="proj-a",
        workspace=WorkspaceInfo(branch_base="main"),
    )
    put_task(hub_dir, "backlog", requeued)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-065.md").exists()
    assert list((hub_dir / "tasks" / "in-progress" / "codex").glob("*.md")) == []


def test_branch_base_cap_blocks_second_task_same_project(scheduler: Scheduler, hub_dir: Path):
    t1 = make_task(id="T-20260826-030", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", t1)

    t2 = make_task(id="T-20260826-031", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", t2)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-031.md").exists()


def test_agent_cap_blocks_second_task_same_agent(scheduler: Scheduler, hub_dir: Path):
    t1 = make_task(id="T-20260826-040", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", t1)

    t2 = make_task(id="T-20260826-041", project="proj-b", assigned_to="claude")
    put_task(hub_dir, "backlog", t2)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-041.md").exists()


def test_agent_cap_applies_to_capability_routing_not_just_assigned_to(scheduler: Scheduler, hub_dir: Path):
    busy = make_task(id="T-20260826-300", project="proj-a", claimed_by="claude", status="in-progress")
    put_task(hub_dir, "in-progress/claude", busy)
    nxt = make_task(id="T-20260826-301", project="proj-b", skills_required=["general"])
    put_task(hub_dir, "backlog", nxt)

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-301.md").exists()
    assert not (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-301.md").exists()
    assert len(list((hub_dir / "tasks" / "in-progress" / "claude").glob("*.md"))) == 1


def test_capability_routing_respects_project_allowed_agents(scheduler: Scheduler, hub_dir: Path):
    task = make_task(id="T-20260826-310", project="proj-c", skills_required=["general"])
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "grok" / "T-20260826-310.md").exists()
    for other in ("claude", "codex"):
        assert list((hub_dir / "tasks" / "in-progress" / other).glob("*.md")) == []


def test_same_tick_branch_base_cap_limits_two_new_candidates_to_one(scheduler: Scheduler, hub_dir: Path):
    t1 = make_task(id="T-20260826-050", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", t1)
    t2 = make_task(id="T-20260826-051", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", t2)

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-050.md").exists()
    assert not (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-051.md").exists()
    assert (hub_dir / "tasks" / "backlog" / "T-20260826-051.md").exists()


def test_same_priority_dispatches_older_task_id_first(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-20260826-050", project="proj-a", skills_required=["general"]))
    put_task(hub_dir, "backlog", make_task(id="T-20260827-051", project="proj-a", skills_required=["general"]))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-050.md").exists()
    assert (hub_dir / "tasks" / "backlog" / "T-20260827-051.md").exists()


def test_assigned_to_bypasses_skill_and_allowed_agents_routing(scheduler: Scheduler, hub_dir: Path):
    task = make_task(
        id="T-20260826-060",
        project="proj-c",
        skills_required=["nonexistent-skill"],
        assigned_to="claude",
    )
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-060.md").exists()


def test_assigned_to_disabled_agent_stays_backlog(scheduler: Scheduler, hub_dir: Path):
    task = make_task(id="T-20260826-061", project="proj-a", assigned_to="hermes")
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-061.md").exists()


def test_skill_routing_picks_agent_with_matching_skill(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task = make_task(id="T-20260826-070", project="proj-a", skills_required=["rust"])
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-070.md").exists()
    assert not (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-070.md").exists()


def test_no_qualified_agent_stays_in_backlog(scheduler: Scheduler, hub_dir: Path):
    task = make_task(id="T-20260826-080", project="proj-a", skills_required=["swift"])
    put_task(hub_dir, "backlog", task)

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260826-080.md").exists()


def test_priority_ordering_overrides_lexical_id_order(scheduler: Scheduler, hub_dir: Path):
    low_priority_small_id = make_task(id="T-20260826-001", priority="P3", project="proj-a", skills_required=["general"])
    high_priority_large_id = make_task(id="T-20260826-005", priority="P0", project="proj-a", skills_required=["general"])
    put_task(hub_dir, "backlog", low_priority_small_id)
    put_task(hub_dir, "backlog", high_priority_large_id)

    scheduler.tick()

    in_progress_ids = {
        p.stem
        for p in list((hub_dir / "tasks" / "in-progress" / "claude").glob("*.md"))
        + list((hub_dir / "tasks" / "in-progress" / "codex").glob("*.md"))
    }
    assert "T-20260826-005" in in_progress_ids
    assert "T-20260826-001" not in in_progress_ids


@pytest.mark.parametrize("higher,lower", [("P0", "P1"), ("P1", "P2"), ("P2", "P3")])
def test_priority_rank_is_ordered_between_every_adjacent_pair(
    scheduler: Scheduler, hub_dir: Path, higher: str, lower: str
):
    put_task(
        hub_dir,
        "backlog",
        make_task(id="T-20260826-331", priority=lower, project="proj-a", skills_required=["general"]),
    )
    put_task(
        hub_dir,
        "backlog",
        make_task(id="T-20260826-330", priority=higher, project="proj-a", skills_required=["general"]),
    )
    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-330.md").exists()
    assert (hub_dir / "tasks" / "backlog" / "T-20260826-331.md").exists()


def test_research_task_routes_to_the_research_agent(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-20260827-001", type="research", skills_required=["research"]))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "agy" / "T-20260827-001.md").exists()


def test_coding_task_never_routes_to_the_research_agent(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-20260827-002", project="proj-a", skills_required=[]))
    put_task(hub_dir, "backlog", make_task(id="T-20260827-003", project="proj-b", skills_required=[]))

    scheduler.tick()

    assert not (hub_dir / "tasks" / "in-progress" / "agy" / "T-20260827-002.md").exists()
    assert not (hub_dir / "tasks" / "in-progress" / "agy" / "T-20260827-003.md").exists()


def test_research_task_never_routes_to_a_code_agent(scheduler: Scheduler, hub_dir: Path):
    put_task(hub_dir, "backlog", make_task(id="T-20260827-004", type="research", skills_required=[]))

    scheduler.tick()

    for agent in ("claude", "codex", "grok"):
        assert not (hub_dir / "tasks" / "in-progress" / agent / "T-20260827-004.md").exists()
    assert (hub_dir / "tasks" / "in-progress" / "agy" / "T-20260827-004.md").exists()


def test_assigned_to_cannot_force_a_task_type_the_agent_does_not_support(
    scheduler: Scheduler, hub_dir: Path
):
    put_task(hub_dir, "backlog", make_task(id="T-20260827-005", assigned_to="agy", skills_required=[]))
    put_task(
        hub_dir,
        "backlog",
        make_task(id="T-20260827-006", type="research", assigned_to="claude", skills_required=[]),
    )

    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-20260827-005.md").exists()
    assert (hub_dir / "tasks" / "backlog" / "T-20260827-006.md").exists()


def test_in_progress_review_task_does_not_occupy_a_write_slot(scheduler: Scheduler, hub_dir: Path):
    running_review = make_task(
        id="T-20260826-066",
        project="proj-a",
        type="review",
        claimed_by="claude",
        status="in-progress",
    )
    put_task(hub_dir, "in-progress/claude", running_review)
    put_task(hub_dir, "backlog", make_task(id="T-20260826-067", project="proj-a"))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-067.md").exists()


def test_review_dispatched_in_the_same_tick_does_not_consume_the_write_slot(
    scheduler: Scheduler, hub_dir: Path
):
    put_task(hub_dir, "backlog", make_task(id="T-20260826-068", project="proj-a", type="review"))
    put_task(hub_dir, "backlog", make_task(id="T-20260826-069", project="proj-a"))

    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-068.md").exists()
    assert (hub_dir / "tasks" / "in-progress" / "codex" / "T-20260826-069.md").exists()
    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
