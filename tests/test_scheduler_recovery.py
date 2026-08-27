from __future__ import annotations

from pathlib import Path

from agenthub import hubfs
from agenthub.scheduler import PollResult, Scheduler
from conftest import FakeClock, FakeRunner, dispatch_task, hub_report_block, make_task, put_task, read_events


def test_orphan_recovery_on_startup_scan_when_process_is_dead(hub_dir: Path, clock: FakeClock):
    runner_a = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner_a, clock)

    task_id = "T-20260826-200"
    in_progress_path = dispatch_task(scheduler_a, hub_dir, task_id)
    assert in_progress_path.exists()

    runner_b = FakeRunner(clock)
    scheduler_b = Scheduler.from_hub_dir(hub_dir, runner_b, clock)
    scheduler_b.startup_scan()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 1
    assert task.claimed_by is None

    assert len(runner_b.kill_calls) == 0
    assert len(runner_b.checkpoint_calls) == 1

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "agent_exited" for e in events)
    assert any(e.event == "task_requeued" for e in events)


def test_startup_scan_leaves_alive_process_in_progress_and_reconnects_handle(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner, clock)

    task_id = "T-20260826-201"
    in_progress_path = dispatch_task(scheduler_a, hub_dir, task_id)
    assert in_progress_path.exists()

    scheduler_b = Scheduler.from_hub_dir(hub_dir, runner, clock)
    scheduler_b.startup_scan()

    assert in_progress_path.exists()
    assert not (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()

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
                        "summary": "ok",
                        "report_md": "fine",
                        "pr_url": "https://x/1",
                    }
                ),
            )
        ],
    )
    scheduler_b.tick()

    assert (hub_dir / "tasks" / "review" / f"{task_id}.md").exists()


def test_startup_scan_hands_the_adopted_run_back_to_the_runner(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner, clock)
    task_id = "T-20260826-202"
    dispatch_task(scheduler_a, hub_dir, task_id)

    Scheduler.from_hub_dir(hub_dir, runner, clock).startup_scan()

    assert [(call[0], call[1]) for call in runner.adopt_calls] == [(task_id, "claude")]


def test_startup_scan_does_not_adopt_a_dead_run(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner, clock)
    task_id = "T-20260826-203"
    dispatch_task(scheduler_a, hub_dir, task_id)
    runner.mark_dead(1000)

    Scheduler.from_hub_dir(hub_dir, runner, clock).startup_scan()

    assert runner.adopt_calls == []


def test_startup_scan_does_not_adopt_a_handle_belonging_to_another_task(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)
    scheduler_a = Scheduler.from_hub_dir(hub_dir, runner, clock)
    dispatch_task(scheduler_a, hub_dir, "T-20260826-360")

    old_path = hub_dir / "tasks" / "in-progress" / "claude" / "T-20260826-360.md"
    old = hubfs.read_task(old_path)
    old_path.unlink()
    other = make_task(
        id="T-20260826-361", claimed_by="claude", status="in-progress", claimed_at=old.claimed_at
    )
    put_task(hub_dir, "in-progress/claude", other)

    Scheduler.from_hub_dir(hub_dir, runner, clock).startup_scan()

    requeued = hub_dir / "tasks" / "backlog" / "T-20260826-361.md"
    assert requeued.exists()
    assert hubfs.read_task(requeued).generation == 1


def test_timeout_kills_the_process_group_not_the_pid(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock
):
    task_id = "T-20260826-401"
    dispatch_task(scheduler, hub_dir, task_id)
    clock.advance(minutes=121)
    scheduler.tick()
    assert runner.kill_calls[0].pgid == runner.kill_calls[0].pid + 5000


def test_heartbeat_marks_idle_working_and_offline(hub_dir: Path, runner: FakeRunner, clock: FakeClock):
    scheduler = Scheduler.from_hub_dir(hub_dir, runner, clock)

    scheduler.tick()
    idle_status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert idle_status is not None
    assert idle_status.state == "idle"
    assert idle_status.task_id is None

    offline_status = hubfs.read_status(hub_dir / "status" / "hermes.json")
    assert offline_status is not None
    assert offline_status.state == "offline"

    task_id = "T-20260826-210"
    dispatch_task(scheduler, hub_dir, task_id)

    working_status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert working_status is not None
    assert working_status.state == "working"
    assert working_status.task_id == task_id

    first_heartbeat = working_status.heartbeat_at
    clock.advance(minutes=1)
    scheduler.tick()
    second_status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert second_status is not None
    assert second_status.heartbeat_at > first_heartbeat


def test_spawn_failure_requeues_task_instead_of_crashing_tick(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)

    def explode(task, agent_name, workspace_dir):
        raise RuntimeError("provision exploded")

    runner.spawn = explode
    scheduler = Scheduler.from_hub_dir(hub_dir, runner, clock)
    put_task(hub_dir, "backlog", make_task(id="T-20260826-500"))

    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / "T-20260826-500.md"
    assert dest.exists()
    requeued = hubfs.read_task(dest)
    assert requeued.generation == 1
    assert requeued.claimed_by is None
    assert requeued.workspace.branch is None
    events = [e for e in read_events(hub_dir) if e.task_id == "T-20260826-500"]
    assert any(e.event == "spawn_failed" and "provision exploded" in e.detail.get("error", "") for e in events)
    assert any(e.event == "task_requeued" for e in events)


def test_spawn_failure_past_max_generation_goes_blocked(hub_dir: Path, clock: FakeClock):
    runner = FakeRunner(clock)

    def explode(task, agent_name, workspace_dir):
        raise RuntimeError("still broken")

    runner.spawn = explode
    scheduler = Scheduler.from_hub_dir(hub_dir, runner, clock)
    put_task(hub_dir, "backlog", make_task(id="T-20260826-501", generation=3))

    scheduler.tick()

    dest = hub_dir / "tasks" / "blocked" / "T-20260826-501.md"
    assert dest.exists()
    assert hubfs.read_task(dest).generation == 4
    events = [e for e in read_events(hub_dir) if e.task_id == "T-20260826-501"]
    assert any(e.event == "task_blocked" for e in events)
