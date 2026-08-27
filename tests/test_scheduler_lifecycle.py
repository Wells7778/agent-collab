from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthub import hubfs
from agenthub.scheduler import PollResult, Scheduler, _extract_hub_reports
from conftest import (
    FakeClock,
    FakeRunner,
    dispatch_task,
    hub_report_block,
    read_events,
    read_messages,
)


def test_checkpoint_then_final_completed_coding_goes_to_review(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-100"
    dispatch_task(scheduler, hub_dir, task_id)

    stdout = "\n".join(
        [
            "some agent chatter",
            hub_report_block(
                {"kind": "checkpoint", "task_id": task_id, "summary": "step 1", "report_md": "did the setup"}
            ),
            "more chatter",
            hub_report_block(
                {
                    "kind": "final",
                    "task_id": task_id,
                    "result": "completed",
                    "summary": "done",
                    "report_md": "all tests pass",
                    "pr_url": "https://example/pr/1",
                }
            ),
        ]
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    dest = hub_dir / "tasks" / "review" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.status == "review"
    assert "did the setup" in task.report_md
    assert "all tests pass" in task.report_md
    assert "https://example/pr/1" in task.report_md

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "task_checkpoint" for e in events)
    assert any(e.event == "task_review_ready" and e.detail.get("result") == "completed" for e in events)


def test_final_completed_review_type_goes_to_done(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-101"
    dispatch_task(scheduler, hub_dir, task_id, type="review", related_task="T-20260826-100")

    stdout = hub_report_block(
        {"kind": "final", "task_id": task_id, "result": "completed", "summary": "reviewed", "report_md": "looks good"}
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    dest = hub_dir / "tasks" / "done" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.status == "done"
    assert "PR:" not in task.report_md

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "task_done" for e in events)


@pytest.mark.parametrize("task_type", ["coding", "review"])
def test_final_failed_always_goes_to_review(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, task_type: str):
    task_id = f"T-20260826-{102 if task_type == 'coding' else 103}"
    dispatch_task(scheduler, hub_dir, task_id, type=task_type)

    stdout = hub_report_block(
        {"kind": "final", "task_id": task_id, "result": "failed", "summary": "broke", "report_md": "tests red"}
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    dest = hub_dir / "tasks" / "review" / f"{task_id}.md"
    assert dest.exists()

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "task_review_ready" and e.detail.get("result") == "failed" for e in events)


def test_blocked_writes_message_and_moves_task(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-110"
    dispatch_task(scheduler, hub_dir, task_id)

    stdout = hub_report_block(
        {"kind": "blocked", "task_id": task_id, "question": "which env var should I use?"}
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    dest = hub_dir / "tasks" / "blocked" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.status == "blocked"

    messages = read_messages(hub_dir)
    assert len(messages) == 1
    assert "which env var should I use?" in messages[0].read_text()

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "task_blocked" for e in events)


def test_no_legal_conclusion_requeues_with_generation_bump(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-120"
    dispatch_task(scheduler, hub_dir, task_id)

    stdout = hub_report_block(
        {"kind": "checkpoint", "task_id": task_id, "summary": "partial", "report_md": "still working"}
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 1
    assert task.claimed_by is None
    assert task.claimed_at is None
    assert task.workspace.branch is None
    assert "still working" in task.report_md

    assert len(runner.kill_calls) == 0
    assert len(runner.checkpoint_calls) == 1

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "agent_exited" for e in events)
    assert any(e.event == "task_requeued" for e in events)


def test_checkpoint_after_final_makes_session_have_no_conclusion(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-320"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = "\n".join(
        [
            hub_report_block(
                {
                    "kind": "final",
                    "task_id": task_id,
                    "result": "completed",
                    "summary": "done",
                    "report_md": "all green",
                    "pr_url": "https://x/1",
                }
            ),
            hub_report_block(
                {
                    "kind": "checkpoint",
                    "task_id": task_id,
                    "summary": "actually still working",
                    "report_md": "kept going",
                }
            ),
        ]
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    assert not (hub_dir / "tasks" / "review" / f"{task_id}.md").exists()
    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    requeued = hubfs.read_task(dest)
    assert requeued.generation == 1
    assert "kept going" in requeued.report_md


def test_multiline_json_hub_report_block_is_parsed(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-390"
    dispatch_task(scheduler, hub_dir, task_id)
    payload = json.dumps(
        {
            "kind": "final",
            "task_id": task_id,
            "result": "completed",
            "summary": "ok",
            "report_md": "line1\nline2",
            "pr_url": "https://x/1",
        },
        indent=2,
        ensure_ascii=False,
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=f"```hub-report\n{payload}\n```")])

    scheduler.tick()

    dest = hub_dir / "tasks" / "review" / f"{task_id}.md"
    assert dest.exists()
    assert "line1\nline2" in hubfs.read_task(dest).report_md


def test_agent_returns_to_idle_after_task_finishes(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-380"
    dispatch_task(scheduler, hub_dir, task_id)
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

    scheduler.tick()

    status = hubfs.read_status(hub_dir / "status" / "claude.json")
    assert status is not None
    assert status.state == "idle"
    assert status.task_id is None
    assert status.pid is None


def test_garbage_stdout_treated_as_dead(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-121"
    dispatch_task(scheduler, hub_dir, task_id)

    runner.script_poll(task_id, [PollResult(exited=True, stdout="segfault, no report at all")])

    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    assert hubfs.read_task(dest).generation == 1


def test_invalid_json_block_is_ignored_valid_final_still_processed(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-122"
    dispatch_task(scheduler, hub_dir, task_id)

    stdout = "\n".join(
        [
            "```hub-report",
            "{not valid json,,,",
            "```",
            hub_report_block(
                {"kind": "final", "task_id": task_id, "result": "completed", "summary": "ok", "report_md": "fine", "pr_url": "https://x/1"}
            ),
        ]
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    assert (hub_dir / "tasks" / "review" / f"{task_id}.md").exists()


def test_timeout_kills_checkpoints_and_requeues(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock):
    task_id = "T-20260826-130"
    dispatch_task(scheduler, hub_dir, task_id)

    clock.advance(minutes=121)
    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 1

    assert len(runner.kill_calls) == 1
    assert len(runner.checkpoint_calls) == 1

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "agent_timeout" for e in events)
    assert any(e.event == "task_requeued" for e in events)


@pytest.mark.parametrize("elapsed,should_requeue", [(119, False), (120, False), (121, True)])
def test_timeout_boundary(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner, clock: FakeClock, elapsed: int, should_requeue: bool
):
    task_id = "T-20260826-340"
    dispatch_task(scheduler, hub_dir, task_id)
    clock.advance(minutes=elapsed)

    scheduler.tick()

    in_progress = hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md"
    backlog = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert backlog.exists() is should_requeue
    assert in_progress.exists() is (not should_requeue)
    assert len(runner.kill_calls) == (1 if should_requeue else 0)


def test_max_generation_exceeded_moves_to_blocked_with_message(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-140"
    dispatch_task(scheduler, hub_dir, task_id, generation=3)

    dispatched = hubfs.read_task(hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md")
    assert dispatched.workspace.branch == f"agent/claude/{task_id}-g3"

    runner.script_poll(task_id, [PollResult(exited=True, stdout="crash, no report")])
    scheduler.tick()

    dest = hub_dir / "tasks" / "blocked" / f"{task_id}.md"
    assert dest.exists()
    task = hubfs.read_task(dest)
    assert task.generation == 4
    assert task.status == "blocked"

    messages = read_messages(hub_dir)
    assert len(messages) == 1
    assert "max_generation" in messages[0].read_text() or "重試代數" in messages[0].read_text()

    events = [e for e in read_events(hub_dir) if e.task_id == task_id]
    assert any(e.event == "task_blocked" and e.detail.get("reason") == "max_generation_exceeded" for e in events)


def test_generation_at_boundary_still_requeues_not_blocks(scheduler: Scheduler, hub_dir: Path, runner: FakeRunner):
    task_id = "T-20260826-141"
    dispatch_task(scheduler, hub_dir, task_id, generation=2)

    runner.script_poll(task_id, [PollResult(exited=True, stdout="crash, no report")])
    scheduler.tick()

    dest = hub_dir / "tasks" / "backlog" / f"{task_id}.md"
    assert dest.exists()
    assert hubfs.read_task(dest).generation == 3


def test_agent_exit_without_any_report_emits_task_no_report(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-180"
    dispatch_task(scheduler, hub_dir, task_id)
    runner.script_poll(task_id, [PollResult(exited=True, stdout="chatter with no report at all")])

    scheduler.tick()

    events = [e for e in read_events(hub_dir) if e.event == "task_no_report"]
    assert [e.task_id for e in events] == [task_id]
    assert events[0].detail["report_blocks"] == 0
    assert events[0].detail["parse_errors"] == 0
    assert (hub_dir / "tasks" / "backlog" / f"{task_id}.md").exists()


def test_malformed_report_block_emits_report_parse_failed_and_counts_toward_no_report(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-181"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = "```hub-report\n{not valid json,,,}\n```"
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    events = read_events(hub_dir)
    parse_failures = [e for e in events if e.event == "report_parse_failed"]
    assert [e.task_id for e in parse_failures] == [task_id]
    assert "invalid json" in parse_failures[0].detail["error"]

    no_report = [e for e in events if e.event == "task_no_report"]
    assert no_report[0].detail["report_blocks"] == 1
    assert no_report[0].detail["parse_errors"] == 1


def test_literal_newlines_inside_report_md_are_accepted(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-184"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = (
        "```hub-report\n"
        '{"kind": "final", "task_id": "' + task_id + '", "result": "completed",'
        ' "summary": "s", "report_md": "## 標題\n- 第一行\n- 第二行", "pr_url": null}'
        "\n```"
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    events = read_events(hub_dir)
    assert [e.event for e in events if e.event == "report_parse_failed"] == []
    assert [e.task_id for e in events if e.event == "task_review_ready"] == [task_id]
    task = hubfs.read_task(hub_dir / "tasks" / "review" / f"{task_id}.md")
    assert "## 標題\n- 第一行\n- 第二行" in task.report_md


def test_extract_hub_reports_keeps_literal_newlines_verbatim():
    report_md = "## 標題\n- 第一行\n- 第二行"
    block = (
        "```hub-report\n"
        '{"kind": "checkpoint", "task_id": "T-1", "summary": "s",'
        ' "report_md": "' + report_md + '"}'
        "\n```"
    )

    reports, errors = _extract_hub_reports(block)

    assert errors == []
    assert [r.report_md for r in reports] == [report_md]


def test_extract_hub_reports_survives_a_code_fence_inside_report_md():
    report_md = "看這段:\n```python\nx = 1\n```\n結束"
    block = (
        "```hub-report\n"
        '{"kind": "checkpoint", "task_id": "T-1", "summary": "s",'
        ' "report_md": "' + report_md + '"}'
        "\n```"
    )

    reports, errors = _extract_hub_reports(block)

    assert errors == []
    assert [r.report_md for r in reports] == [report_md]


def test_extract_hub_reports_ignores_trailing_junk_after_the_json_object():
    block = (
        "```hub-report\n"
        '{"kind": "checkpoint", "task_id": "T-1", "summary": "s", "report_md": "m"}'
        '", "type": "final"}\n```'
    )

    reports, errors = _extract_hub_reports(block)

    assert errors == []
    assert [r.summary for r in reports] == ["s"]


def test_extract_hub_reports_still_rejects_truncated_json():
    block = '```hub-report\n{"kind": "final", "task_id": "T-1"\n```'

    reports, errors = _extract_hub_reports(block)

    assert reports == []
    assert len(errors) == 1
    assert "invalid json" in errors[0]


def test_report_block_failing_schema_validation_is_reported_separately(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-182"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = hub_report_block({"kind": "final", "task_id": task_id, "result": "completed"})
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    parse_failures = [e for e in read_events(hub_dir) if e.event == "report_parse_failed"]
    assert "schema validation failed" in parse_failures[0].detail["error"]


def test_task_no_report_detail_counts_are_not_interchangeable(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-183"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = "\n".join(
        [
            hub_report_block(
                {"kind": "checkpoint", "task_id": task_id, "summary": "s", "report_md": "m"}
            ),
            "```hub-report\n{broken,,,}\n```",
        ]
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    event = next(e for e in read_events(hub_dir) if e.event == "task_no_report")
    assert event.agent == "claude"
    assert event.detail["report_blocks"] == 2
    assert event.detail["parse_errors"] == 1
    assert event.detail["stdout_bytes"] == len(stdout)


def test_every_malformed_block_emits_its_own_report_parse_failed(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-184"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = "```hub-report\n{first,,,}\n```\n```hub-report\n{second,,,}\n```"
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    failures = [e for e in read_events(hub_dir) if e.event == "report_parse_failed"]
    assert len(failures) == 2
    assert "first" in failures[0].detail["error"]
    assert "second" in failures[1].detail["error"]


def test_malformed_block_is_reported_even_when_a_valid_final_follows(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-185"
    dispatch_task(scheduler, hub_dir, task_id)
    stdout = "\n".join(
        [
            "```hub-report\n{broken,,,}\n```",
            hub_report_block(
                {
                    "kind": "final",
                    "task_id": task_id,
                    "result": "completed",
                    "summary": "done",
                    "report_md": "ok",
                    "pr_url": "https://example/pr/9",
                }
            ),
        ]
    )
    runner.script_poll(task_id, [PollResult(exited=True, stdout=stdout)])

    scheduler.tick()

    events = read_events(hub_dir)
    assert [e.event for e in events if e.event == "report_parse_failed"] == ["report_parse_failed"]
    assert not [e for e in events if e.event == "task_no_report"]
    assert (hub_dir / "tasks" / "review" / f"{task_id}.md").exists()


def test_rate_limited_exit_is_not_counted_as_a_missing_report(
    scheduler: Scheduler, hub_dir: Path, runner: FakeRunner
):
    task_id = "T-20260826-186"
    dispatch_task(scheduler, hub_dir, task_id)
    runner.script_poll(
        task_id, [PollResult(exited=True, stdout="", rate_limited=True, rate_limit_reset_at=None)]
    )

    scheduler.tick()

    events = read_events(hub_dir)
    assert not [e for e in events if e.event == "task_no_report"]
    assert [e.event for e in events if e.event == "agent_rate_limited"] == ["agent_rate_limited"]


def test_truncate_keeps_a_readable_prefix_of_the_offending_block():
    from agenthub.scheduler import _truncate

    assert _truncate("short block") == "short block"
    assert _truncate("a\n  b\tc") == "a b c"
    long_block = "x" * 500
    truncated = _truncate(long_block)
    assert truncated.startswith("x" * 200)
    assert len(truncated) == 201
    assert truncated.endswith("…")
