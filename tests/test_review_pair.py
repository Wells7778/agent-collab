from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agenthub import hubfs
from agenthub.scheduler import PollResult, Scheduler
from conftest import FakeClock, FakeRunner, hub_report_block, make_task, put_task, read_events

ROLE_CONFIG: dict = {
    "workspaces_root": "~/agent-workspaces-test",
    "max_concurrent_global": 4,
    "max_concurrent_per_branch_base": 2,
    "max_concurrent_per_agent": 1,
    "task_timeout_minutes": 120,
    "heartbeat_seconds": 60,
    "worktree_retention_days": 7,
    "max_generation": 3,
    "agents": {
        "implementer": {
            "enabled": True,
            "runtime": "claude",
            "skills": ["general", "python"],
            "task_types": ["coding"],
        },
        "verifier": {
            "enabled": True,
            "runtime": "codex",
            "skills": ["general", "python"],
            "task_types": ["review"],
        },
        "test-auditor": {
            "enabled": True,
            "runtime": "codex",
            "skills": ["general", "python"],
            "task_types": ["review"],
        },
    },
}

ROLE_PROJECTS: dict = {
    "proj-a": {
        "repo": "codecommit::us-west-2://proj-a",
        "default_branch": "develop",
        "setup": [],
        "setup_secrets": [],
        "test": [],
        "spec_paths": ["tests/**"],
        "knowledge_paths": [],
        "allowed_agents": ["implementer", "verifier", "test-auditor"],
    }
}


def build(hub_dir: Path, runner: FakeRunner, clock: FakeClock, config=None, projects=None):
    (hub_dir / "config.yaml").write_text(yaml.safe_dump(config or ROLE_CONFIG, sort_keys=False))
    (hub_dir / "projects.yaml").write_text(yaml.safe_dump(projects or ROLE_PROJECTS, sort_keys=False))
    return Scheduler.from_hub_dir(hub_dir, runner, clock)


def complete_coding_task(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, task_id: str, changed: list[str]
) -> None:
    put_task(hub_dir, "backlog", make_task(id=task_id, project="proj-a"))
    scheduler.tick()
    runner.changed_files_by_task[task_id] = changed
    runner.script_poll(
        task_id,
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": task_id,
                        "result": "completed",
                        "summary": "done",
                        "report_md": "impl notes",
                        "pr_url": "https://example/pr/1",
                    }
                ),
            )
        ],
    )
    scheduler.tick()


def test_spec_change_creates_exactly_two_review_tasks_on_distinct_roles(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-1", ["agenthub/x.py", "tests/test_x.py"])

    created = sorted(p.name for p in (hub_dir / "tasks" / "backlog").glob("*.md"))
    assert created == ["T-1-review-test-auditor.md", "T-1-review-verifier.md"]

    reviewers = sorted(
        hubfs.read_task(hub_dir / "tasks" / "backlog" / name).assigned_to for name in created
    )
    assert reviewers == ["test-auditor", "verifier"]
    for name in created:
        review = hubfs.read_task(hub_dir / "tasks" / "backlog" / name)
        assert review.type == "review"
        assert review.related_task == "T-1"
        assert "https://example/pr/1" in review.requirement_md


def test_no_review_pair_when_no_spec_file_changed(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-2", ["README.md", "agenthub/x.py"])

    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
    assert not [e for e in read_events(hub_dir) if e.event == "review_task_created"]


def test_failed_implementation_does_not_create_a_review_pair(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    put_task(hub_dir, "backlog", make_task(id="T-3", project="proj-a"))
    scheduler.tick()
    runner.changed_files_by_task["T-3"] = ["tests/test_x.py"]
    runner.script_poll(
        "T-3",
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": "T-3",
                        "result": "failed",
                        "summary": "gave up",
                        "report_md": "notes",
                    }
                ),
            )
        ],
    )
    scheduler.tick()

    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []


def test_reviewer_sharing_the_implementer_runtime_is_not_eligible(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    config = {
        **ROLE_CONFIG,
        "agents": {
            **ROLE_CONFIG["agents"],
            "test-auditor": {**ROLE_CONFIG["agents"]["test-auditor"], "runtime": "claude"},
        },
    }
    scheduler = build(hub_dir, runner, clock, config=config)
    complete_coding_task(scheduler, hub_dir, runner, "T-4", ["tests/test_x.py"])

    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
    unavailable = [e for e in read_events(hub_dir) if e.event == "review_pair_unavailable"]
    assert [e.task_id for e in unavailable] == ["T-4"]
    assert unavailable[0].detail["eligible"] == ["verifier"]


def test_completed_review_appends_its_report_to_the_original_task(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-5", ["tests/test_x.py"])

    review_id = "T-5-review-verifier"
    scheduler.tick()
    runner.script_poll(
        review_id,
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": review_id,
                        "result": "completed",
                        "summary": "審過了",
                        "report_md": "條件 1 PASS,條件 2 PASS",
                    }
                ),
            )
        ],
    )
    scheduler.tick()

    original = hubfs.read_task(hub_dir / "tasks" / "review" / "T-5.md")
    assert "互審回報(verifier)" in original.report_md
    assert "條件 1 PASS" in original.report_md
    assert (hub_dir / "tasks" / "done" / f"{review_id}.md").exists()


def test_original_task_waits_in_review_for_a_human_after_both_reviews_pass(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-6", ["tests/test_x.py"])

    for reviewer in ("verifier", "test-auditor"):
        review_id = f"T-6-review-{reviewer}"
        scheduler.tick()
        runner.script_poll(
            review_id,
            [
                PollResult(
                    exited=True,
                    stdout=hub_report_block(
                        {
                            "kind": "final",
                            "task_id": review_id,
                            "result": "completed",
                            "summary": f"{reviewer} 通過",
                            "report_md": f"{reviewer} 的報告",
                        }
                    ),
                )
            ],
        )
        scheduler.tick()

    assert (hub_dir / "tasks" / "review" / "T-6.md").exists()
    assert not (hub_dir / "tasks" / "done" / "T-6.md").exists()
    original = hubfs.read_task(hub_dir / "tasks" / "review" / "T-6.md")
    assert "verifier 的報告" in original.report_md
    assert "test-auditor 的報告" in original.report_md


def test_review_task_created_event_names_the_reviewer_and_the_related_task(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-7", ["tests/test_x.py"])

    created = [e for e in read_events(hub_dir) if e.event == "review_task_created"]
    assert sorted(e.agent for e in created) == ["test-auditor", "verifier"]
    assert {e.task_id for e in created} == {"T-7-review-verifier", "T-7-review-test-auditor"}
    assert all(e.detail["related_task"] == "T-7" for e in created)


def test_implementer_that_can_also_review_never_reviews_itself(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    config = {
        **ROLE_CONFIG,
        "agents": {
            **ROLE_CONFIG["agents"],
            "implementer": {
                **ROLE_CONFIG["agents"]["implementer"],
                "task_types": ["coding", "review"],
            },
        },
    }
    scheduler = build(hub_dir, runner, clock, config=config)
    complete_coding_task(scheduler, hub_dir, runner, "T-8", ["tests/test_x.py"])

    created = sorted(p.name for p in (hub_dir / "tasks" / "backlog").glob("*.md"))
    assert "T-8-review-implementer.md" not in created
    assert created == ["T-8-review-test-auditor.md", "T-8-review-verifier.md"]


def test_disabled_reviewer_is_not_eligible(hub_dir: Path, runner: FakeRunner, clock: FakeClock):
    config = {
        **ROLE_CONFIG,
        "agents": {
            **ROLE_CONFIG["agents"],
            "test-auditor": {**ROLE_CONFIG["agents"]["test-auditor"], "enabled": False},
        },
    }
    scheduler = build(hub_dir, runner, clock, config=config)
    complete_coding_task(scheduler, hub_dir, runner, "T-9", ["tests/test_x.py"])

    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
    unavailable = [e for e in read_events(hub_dir) if e.event == "review_pair_unavailable"]
    assert unavailable[0].detail["eligible"] == ["verifier"]


def test_a_role_without_the_review_task_type_is_not_eligible(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    config = {
        **ROLE_CONFIG,
        "agents": {
            **ROLE_CONFIG["agents"],
            "test-auditor": {**ROLE_CONFIG["agents"]["test-auditor"], "task_types": ["research"]},
        },
    }
    scheduler = build(hub_dir, runner, clock, config=config)
    complete_coding_task(scheduler, hub_dir, runner, "T-10", ["tests/test_x.py"])

    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
    unavailable = [e for e in read_events(hub_dir) if e.event == "review_pair_unavailable"]
    assert unavailable[0].detail["eligible"] == ["verifier"]


def test_more_than_two_eligible_reviewers_picks_the_first_two_in_allowed_order(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    config = {
        **ROLE_CONFIG,
        "agents": {
            **ROLE_CONFIG["agents"],
            "third-reviewer": {
                "enabled": True,
                "runtime": "gemini",
                "skills": ["general", "python"],
                "task_types": ["review"],
            },
        },
    }
    projects = {
        "proj-a": {
            **ROLE_PROJECTS["proj-a"],
            "allowed_agents": ["implementer", "verifier", "test-auditor", "third-reviewer"],
        }
    }
    scheduler = build(hub_dir, runner, clock, config=config, projects=projects)
    complete_coding_task(scheduler, hub_dir, runner, "T-11", ["tests/test_x.py"])

    created = sorted(p.name for p in (hub_dir / "tasks" / "backlog").glob("*.md"))
    assert created == ["T-11-review-test-auditor.md", "T-11-review-verifier.md"]


def test_review_report_is_dropped_silently_when_the_original_task_is_gone(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    complete_coding_task(scheduler, hub_dir, runner, "T-12", ["tests/test_x.py"])
    (hub_dir / "tasks" / "review" / "T-12.md").unlink()

    review_id = "T-12-review-verifier"
    scheduler.tick()
    runner.script_poll(
        review_id,
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": review_id,
                        "result": "completed",
                        "summary": "s",
                        "report_md": "r",
                    }
                ),
            )
        ],
    )
    scheduler.tick()

    assert (hub_dir / "tasks" / "done" / f"{review_id}.md").exists()


EXPLORE_CONFIG: dict = {
    **ROLE_CONFIG,
    "agents": {
        **ROLE_CONFIG["agents"],
        "explorer": {
            "enabled": True,
            "runtime": "hermes",
            "skills": ["general"],
            "task_types": ["explore"],
        },
    },
}


def test_explore_task_routes_to_the_explorer_and_finishes_in_review(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    projects = {
        "proj-a": {
            **ROLE_PROJECTS["proj-a"],
            "allowed_agents": ["implementer", "verifier", "test-auditor", "explorer"],
        }
    }
    scheduler = build(hub_dir, runner, clock, config=EXPLORE_CONFIG, projects=projects)
    put_task(hub_dir, "backlog", make_task(id="T-13", project="proj-a", type="explore"))
    scheduler.tick()

    assert (hub_dir / "tasks" / "in-progress" / "explorer" / "T-13.md").exists()

    runner.script_poll(
        "T-13",
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": "T-13",
                        "result": "completed",
                        "summary": "盤點完成",
                        "report_md": "agenthub/scheduler.py:198 — 寫入槽計數",
                    }
                ),
            )
        ],
    )
    scheduler.tick()

    assert (hub_dir / "tasks" / "review" / "T-13.md").exists()
    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []


def test_a_coding_role_never_receives_an_explore_task(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    put_task(hub_dir, "backlog", make_task(id="T-14", project="proj-a", type="explore"))
    scheduler.tick()

    assert (hub_dir / "tasks" / "backlog" / "T-14.md").exists()


def test_undeterminable_diff_is_reported_instead_of_silently_skipping_review(
    hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    scheduler = build(hub_dir, runner, clock)
    put_task(hub_dir, "backlog", make_task(id="T-15", project="proj-a"))
    scheduler.tick()
    runner.changed_files_by_task["T-15"] = None
    runner.script_poll(
        "T-15",
        [
            PollResult(
                exited=True,
                stdout=hub_report_block(
                    {
                        "kind": "final",
                        "task_id": "T-15",
                        "result": "completed",
                        "summary": "done",
                        "report_md": "notes",
                    }
                ),
            )
        ],
    )
    scheduler.tick()

    unavailable = [e for e in read_events(hub_dir) if e.event == "review_pair_unavailable"]
    assert [e.task_id for e in unavailable] == ["T-15"]
    assert unavailable[0].detail["reason"] == "could not determine which files changed"
    assert list((hub_dir / "tasks" / "backlog").glob("*.md")) == []
