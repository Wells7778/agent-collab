from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agenthub import hubfs
from agenthub.scheduler import PollResult, Scheduler
from conftest import FakeClock, FakeRunner, dispatch_task, hub_report_block, read_events


def rate_limited_poll(reset_at: datetime | None = None) -> PollResult:
    return PollResult(exited=True, stdout="", rate_limited=True, rate_limit_reset_at=reset_at)


def test_rate_limited_exit_requeues_without_burning_generation(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-600"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    runner.script_poll(task_id, [rate_limited_poll()])
    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 0
    assert task.claimed_by is None
    assert task.workspace.branch is None

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "agent_rate_limited" and e.detail.get("cooldown_until") for e in events)
    assert any(e.event == "task_requeued" and e.detail.get("generation") == 0 for e in events)
    assert not any(e.event == "agent_exited" for e in events)


def test_assigned_agent_in_cooldown_is_skipped_until_expiry(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-601"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    runner.script_poll(task_id, [rate_limited_poll()])
    scheduler.tick()

    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()

    clock.advance(minutes=31)
    scheduler.tick()
    in_progress = hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md"
    assert in_progress.exists()
    assert hubfs.read_task(in_progress).generation == 0


def test_rate_limited_task_fails_over_to_another_allowed_agent(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-602"
    dispatch_task(scheduler, hub_dir, task_id)
    runner.script_poll(task_id, [rate_limited_poll()])
    scheduler.tick()

    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "codex" / f"{task_id}.md").exists()


def test_reset_at_overrides_default_cooldown_and_status_shows_resting(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-603"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    reset_at = clock.now() + timedelta(hours=2)
    runner.script_poll(task_id, [rate_limited_poll(reset_at)])
    scheduler.tick()

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.state == "resting"
    assert status.cooldown_until == reset_at

    clock.advance(minutes=31)
    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()

    clock.advance(minutes=91)
    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md").exists()
    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.state == "working"


def test_cooldown_survives_daemon_restart_via_startup_scan(hub_dir: Path, clock: FakeClock):
    runner_a = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner_a, clock)
    task_id = "T-20260826-604"
    dispatch_task(scheduler_a, hub_dir, task_id, assigned_to="claude")
    runner_a.script_poll(task_id, [rate_limited_poll(clock.now() + timedelta(hours=2))])
    scheduler_a.tick()

    runner_b = FakeRunner(clock)
    scheduler_b = Scheduler.from_hub_dir(hub_dir, runner_b, clock)
    scheduler_b.startup_scan()

    clock.advance(minutes=31)
    scheduler_b.tick()
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()

    clock.advance(minutes=91)
    scheduler_b.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md").exists()


def test_default_cooldown_is_exactly_config_minutes(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-607"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    started_at = clock.now()
    runner.script_poll(task_id, [rate_limited_poll()])
    scheduler.tick()

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.cooldown_until == started_at + timedelta(minutes=30)

    clock.advance(minutes=29)
    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()


def test_agent_is_dispatchable_exactly_at_cooldown_expiry(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-608"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    reset_at = clock.now() + timedelta(minutes=45)
    runner.script_poll(task_id, [rate_limited_poll(reset_at)])
    scheduler.tick()

    clock.advance(minutes=44, seconds=59)
    scheduler.tick()
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()

    clock.advance(seconds=1)
    scheduler.tick()
    assert (hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md").exists()


def test_stale_reset_at_falls_back_to_default_cooldown(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-610"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    started_at = clock.now()
    runner.script_poll(task_id, [rate_limited_poll(started_at - timedelta(hours=1))])
    scheduler.tick()

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.state == "resting"
    assert status.cooldown_until == started_at + timedelta(minutes=30)
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()


def test_blocked_report_takes_precedence_over_rate_limit_flag(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-609"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    payload = {
        "kind": "blocked",
        "task_id": task_id,
        "question": "要用哪個 API?",
    }
    runner.script_poll(
        task_id,
        [PollResult(exited=True, stdout=hub_report_block(payload), rate_limited=True)],
    )
    scheduler.tick()

    assert (hub_dir / "tasks" / "blocked" / f"{task_id}.md").exists()
    assert not (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()


def test_final_report_takes_precedence_over_rate_limit_flag(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-605"
    dispatch_task(scheduler, hub_dir, task_id)
    payload = {
        "kind": "final",
        "task_id": task_id,
        "result": "completed",
        "summary": "ok",
        "report_md": "done",
    }
    runner.script_poll(
        task_id,
        [PollResult(exited=True, stdout=hub_report_block(payload), rate_limited=True)],
    )
    scheduler.tick()

    assert (hub_dir / "tasks" / "review" / f"{task_id}.md").exists()
    assert not (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()


def test_checkpoint_is_preserved_on_rate_limited_requeue(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-606"
    dispatch_task(scheduler, hub_dir, task_id, assigned_to="claude")
    payload = {
        "kind": "checkpoint",
        "task_id": task_id,
        "summary": "half done",
        "report_md": "progress so far",
    }
    runner.script_poll(
        task_id,
        [PollResult(exited=True, stdout=hub_report_block(payload), rate_limited=True)],
    )
    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 0
    assert "progress so far" in task.report_md
