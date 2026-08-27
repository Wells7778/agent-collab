from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agenthub import hubfs, probe, provision
from agenthub.claude_runner import ClaudeRunner, _detect_rate_limit, _parse_reset_epoch
from agenthub.dashboard_api import create_app
from agenthub.schema import HubConfig, ProjectConfig
from agenthub.scheduler import Scheduler
from conftest import DEFAULT_CONFIG, REPO_ROOT, FakeClock, make_task, put_task


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _make_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "--initial-branch=develop", "."], remote)
    seed = tmp_path / "seed"
    _git(["clone", str(remote), str(seed)], tmp_path)
    (seed / "README.md").write_text("seed\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"], seed)
    _git(["push", "-q", "origin", "HEAD:develop"], seed)
    return remote


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude",
        "#!/bin/sh\n"
        'ID=$(grep "^id:" | tail -1 | sed "s/^id: *//")\n'
        "printf '```hub-report\\n"
        '{"kind": "final", "task_id": "%s", "result": "completed", '
        '"summary": "ok", "report_md": "done by fake", "pr_url": "https://example/pr/9"}'
        "\\n```\\n' \"$ID\"\n",
    )


@pytest.fixture
def fake_stubborn(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-stubborn",
        "#!/bin/sh\ncat >/dev/null\nsleep 30 &\nsleep 30\n",
    )


@pytest.fixture
def runner_hub(hub_dir: Path) -> Path:
    (hub_dir / "PROTOCOL.md").write_text((REPO_ROOT / "PROTOCOL.md").read_text())
    return hub_dir


def _project_cfg(bare_repo: Path, **overrides) -> ProjectConfig:
    data = dict(
        repo=str(bare_repo),
        default_branch="develop",
        setup=["touch setup-ran"],
        setup_secrets=["touch secrets-ran"],
        test=[],
        knowledge_paths=[],
        allowed_agents=["claude"],
    )
    data.update(overrides)
    return ProjectConfig.model_validate(data)


def _runner_config(command_path: Path, tmp_path: Path, agent: str = "claude") -> HubConfig:
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    data["workspaces_root"] = str(tmp_path / "workspaces")
    data["agents"][agent]["command"] = [str(command_path)]
    return HubConfig.model_validate(data)


def _branched_task(task_id: str, generation: int = 0, **overrides):
    task = make_task(id=task_id, generation=generation, **overrides)
    branch = f"agent/claude/{task_id}-g{generation}"
    return task.model_copy(update={"workspace": task.workspace.model_copy(update={"branch": branch})})


def _wait_exited(runner: ClaudeRunner, handle, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runner.poll(handle)
        if result.exited:
            return result
        time.sleep(0.05)
    raise AssertionError("process did not exit in time")


def test_provision_creates_worktree_branch_and_runs_setup(tmp_path: Path, bare_repo: Path):
    cfg = _project_cfg(bare_repo)
    task = _branched_task("T-P-001")
    ws_root = tmp_path / "workspaces"
    task_dir = ws_root / "proj-a" / "claude" / "T-P-001"

    provision.provision_workspace(ws_root, task_dir, task, cfg)

    assert (task_dir / "README.md").exists()
    assert (task_dir / "secrets-ran").exists()
    assert (task_dir / "setup-ran").exists()
    assert _git(["branch", "--show-current"], task_dir).strip() == "agent/claude/T-P-001-g0"


def test_provision_generation_continuation_from_branch_tip(tmp_path: Path, bare_repo: Path):
    cfg = _project_cfg(bare_repo)
    ws_root = tmp_path / "workspaces"
    task_dir = ws_root / "proj-a" / "claude" / "T-P-002"
    provision.provision_workspace(ws_root, task_dir, _branched_task("T-P-002"), cfg)
    (task_dir / "wip.txt").write_text("wip\n")
    _git(["add", "-A"], task_dir)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "wip-from-g0"], task_dir)

    provision.provision_workspace(ws_root, task_dir, _branched_task("T-P-002", generation=1), cfg)

    assert _git(["branch", "--show-current"], task_dir).strip() == "agent/claude/T-P-002-g1"
    assert (task_dir / "wip.txt").exists()
    assert "wip-from-g0" in _git(["log", "--oneline"], task_dir)


def test_provision_without_branch_raises(tmp_path: Path, bare_repo: Path):
    with pytest.raises(provision.ProvisionError):
        provision.provision_workspace(
            tmp_path / "workspaces",
            tmp_path / "workspaces" / "proj-a" / "claude" / "T-P-003",
            make_task(id="T-P-003"),
            _project_cfg(bare_repo),
        )


def test_provision_failing_setup_step_raises(tmp_path: Path, bare_repo: Path):
    cfg = _project_cfg(bare_repo, setup=["false"])
    with pytest.raises(provision.ProvisionError):
        provision.provision_workspace(
            tmp_path / "workspaces",
            tmp_path / "workspaces" / "proj-a" / "claude" / "T-P-004",
            _branched_task("T-P-004"),
            cfg,
        )


def test_secrets_run_before_setup(tmp_path: Path, bare_repo: Path):
    cfg = _project_cfg(
        bare_repo,
        setup_secrets=["echo secrets >> order.txt"],
        setup=["echo setup >> order.txt"],
    )
    task_dir = tmp_path / "workspaces" / "proj-a" / "claude" / "T-P-010"
    provision.provision_workspace(tmp_path / "workspaces", task_dir, _branched_task("T-P-010"), cfg)
    assert (task_dir / "order.txt").read_text().split() == ["secrets", "setup"]


def test_sync_knowledge_projects_whitelisted_dirs(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    src = tmp_path / "obs" / "shared"
    src.mkdir(parents=True)
    (src / "note.md").write_text("n\n")
    cfg = ProjectConfig.model_validate(
        dict(repo="unused", default_branch="main", knowledge_paths=[str(src)], allowed_agents=[])
    )

    synced = provision.sync_knowledge(hub, "proj-a", cfg)

    assert synced == [hub / "knowledge" / "proj-a" / "shared"]
    assert (hub / "knowledge" / "proj-a" / "shared" / "note.md").exists()

    (src / "note.md").unlink()
    provision.sync_knowledge(hub, "proj-a", cfg)
    assert not (hub / "knowledge" / "proj-a" / "shared" / "note.md").exists()


def test_sync_knowledge_skips_missing_paths(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    good = tmp_path / "obs" / "shared"
    good.mkdir(parents=True)
    (good / "n.md").write_text("n\n")
    cfg = ProjectConfig.model_validate(
        dict(
            repo="unused",
            default_branch="main",
            knowledge_paths=[str(tmp_path / "does-not-exist"), str(good)],
            allowed_agents=[],
        )
    )
    assert provision.sync_knowledge(hub, "proj-a", cfg) == [hub / "knowledge" / "proj-a" / "shared"]
    assert not (hub / "knowledge" / "proj-a" / "does-not-exist").exists()


def test_runner_spawn_polls_stdout_and_injects_prompt(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    task = _branched_task("T-R-001")
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-001"

    handle = runner.spawn(task, "claude", ws)

    assert handle.pgid == os.getpgid(handle.pid)
    assert handle.pgid != os.getpgid(0)
    result = _wait_exited(runner, handle)
    assert '"task_id": "T-R-001"' in result.stdout
    prompt = (ws.parent / "T-R-001.hub" / "prompt-g0.md").read_text()
    assert "hub-report" in prompt
    assert "T-R-001" in prompt
    untracked = {line.split()[-1] for line in _git(["status", "--porcelain"], ws).splitlines()}
    assert untracked == {"secrets-ran", "setup-ran"}


def test_runner_prompt_includes_only_latest_handoff(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    (runner_hub / "handoffs" / "T-R-002.handoff-1.md").write_text("older handoff content\n")
    (runner_hub / "handoffs" / "T-R-002.handoff-2.md").write_text("newest handoff content\n")
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-002"

    handle = runner.spawn(_branched_task("T-R-002"), "claude", ws)
    _wait_exited(runner, handle)

    prompt = (ws.parent / "T-R-002.hub" / "prompt-g0.md").read_text()
    assert "newest handoff content" in prompt
    assert "older handoff content" not in prompt


def test_prompt_picks_highest_numbered_handoff_across_two_digits(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    (runner_hub / "handoffs" / "T-R-020.handoff-2.md").write_text("older handoff content\n")
    (runner_hub / "handoffs" / "T-R-020.handoff-10.md").write_text("newest handoff content\n")
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-020"
    _wait_exited(runner, runner.spawn(_branched_task("T-R-020"), "claude", ws))
    prompt = (ws.parent / "T-R-020.hub" / "prompt-g0.md").read_text()
    assert "newest handoff content" in prompt
    assert "older handoff content" not in prompt


def test_prompt_lists_knowledge_projection_paths(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    src = tmp_path / "obs" / "shared"
    src.mkdir(parents=True)
    (src / "note.md").write_text("n\n")
    cfg = _project_cfg(bare_repo, knowledge_paths=[str(src)])
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": cfg}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-021"
    _wait_exited(runner, runner.spawn(_branched_task("T-R-021"), "claude", ws))
    prompt = (ws.parent / "T-R-021.hub" / "prompt-g0.md").read_text()
    assert "knowledge projection" in prompt
    assert str(runner_hub / "knowledge" / "proj-a" / "shared") in prompt


def test_kill_pgid_terminates_entire_group(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_stubborn: Path
):
    config = _runner_config(fake_stubborn, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-003"
    handle = runner.spawn(_branched_task("T-R-003"), "claude", ws)
    time.sleep(0.3)

    runner.kill_pgid(handle)

    deadline = time.monotonic() + 5
    group_gone = False
    while time.monotonic() < deadline and not group_gone:
        try:
            os.killpg(handle.pgid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            group_gone = True
        except PermissionError:
            time.sleep(0.05)
    assert group_gone
    assert runner.is_alive(handle.pid, handle.started_at) is False


def test_checkpoint_workspace_commits_wip(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-004"
    handle = runner.spawn(_branched_task("T-R-004"), "claude", ws)
    _wait_exited(runner, handle)

    runner.checkpoint_workspace(handle)
    baseline = _git(["log", "--oneline"], ws)

    (ws / "dirty.txt").write_text("d\n")
    runner.checkpoint_workspace(handle)

    log = _git(["log", "--oneline"], ws)
    assert "checkpoint" in log
    assert log != baseline
    assert _git(["status", "--porcelain"], ws).strip() == ""


def test_is_alive_false_for_exited_process(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-005"
    handle = runner.spawn(_branched_task("T-R-005"), "claude", ws)
    _wait_exited(runner, handle)

    assert runner.is_alive(handle.pid, handle.started_at) is False


def test_is_alive_true_for_running_process(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_stubborn: Path
):
    config = _runner_config(fake_stubborn, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-100"
    handle = runner.spawn(_branched_task("T-R-100"), "claude", ws)
    try:
        time.sleep(0.3)
        assert runner.is_alive(handle.pid, handle.started_at) is True
    finally:
        runner.kill_pgid(handle)


def test_is_alive_false_when_started_at_does_not_match_pid(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_stubborn: Path
):
    config = _runner_config(fake_stubborn, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-101"
    handle = runner.spawn(_branched_task("T-R-101"), "claude", ws)
    try:
        time.sleep(0.3)
        assert runner.is_alive(handle.pid, handle.started_at - timedelta(hours=1)) is False
    finally:
        runner.kill_pgid(handle)


def test_is_alive_false_when_process_start_time_unavailable(
    monkeypatch, runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    monkeypatch.setattr("agenthub.claude_runner._process_start_time", lambda pid: None)
    assert runner.is_alive(os.getpid(), FakeClock().now()) is False


def test_live_dict_sweeps_delivered_entries_on_next_spawn(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)

    ws1 = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-006"
    handle1 = runner.spawn(_branched_task("T-R-006"), "claude", ws1)
    _wait_exited(runner, handle1)
    assert handle1.pid in runner._live

    ws2 = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-007"
    runner.spawn(_branched_task("T-R-007"), "claude", ws2)

    assert handle1.pid not in runner._live


def test_probe_reports_availability(tmp_path: Path):
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    data["agents"]["claude"]["probe"] = ["/usr/bin/true"]
    data["agents"]["codex"]["probe"] = [str(tmp_path / "missing-binary")]
    data["agents"]["grok"]["enabled"] = False
    config = HubConfig.model_validate(data)

    results = probe.run_probes(config)

    assert results["claude"] is True
    assert results["codex"] is False
    assert "grok" not in results
    assert "hermes" not in results


def test_full_cycle_backlog_to_review_with_real_runner(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    projects = {"proj-a": _project_cfg(bare_repo)}
    runner = ClaudeRunner(runner_hub, config, projects, term_grace_seconds=1.0)
    scheduler = Scheduler(runner_hub, config, projects, runner, FakeClock())
    put_task(runner_hub, "backlog", make_task(id="T-INT-001"))

    scheduler.tick()
    assert (runner_hub / "tasks" / "in-progress" / "claude" / "T-INT-001.md").exists()

    review_path = runner_hub / "tasks" / "review" / "T-INT-001.md"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not review_path.exists():
        time.sleep(0.1)
        scheduler.tick()

    assert review_path.exists()
    task = hubfs.read_task(review_path)
    assert "done by fake" in task.report_md
    assert "https://example/pr/9" in task.report_md


def test_pr_line_written_by_daemon_is_readable_by_dashboard(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude: Path
):
    config = _runner_config(fake_claude, tmp_path)
    projects = {"proj-a": _project_cfg(bare_repo)}
    scheduler = Scheduler(
        runner_hub,
        config,
        projects,
        ClaudeRunner(runner_hub, config, projects, term_grace_seconds=1.0),
        FakeClock(),
    )
    put_task(runner_hub, "backlog", make_task(id="T-INT-002"))
    scheduler.tick()
    review_path = runner_hub / "tasks" / "review" / "T-INT-002.md"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not review_path.exists():
        time.sleep(0.1)
        scheduler.tick()

    report = hubfs.read_task(review_path).report_md
    assert "PR: https://example/pr/9" in report
    assert hubfs.extract_pr_url(report) == "https://example/pr/9"

    body = TestClient(create_app(runner_hub)).post("/api/tasks/T-INT-002/review", json={}).json()
    assert "https://example/pr/9" in body["requirement_md"]


def test_spawn_env_from_config_reaches_child(runner_hub: Path, tmp_path: Path, bare_repo: Path):
    env_echo = _make_executable(
        tmp_path / "env-echo",
        "#!/bin/sh\ncat >/dev/null\nprintf '%s' \"$HUB_SPAWN_MARKER\"\n",
    )
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    data["workspaces_root"] = str(tmp_path / "workspaces")
    data["agents"]["claude"]["command"] = [str(env_echo)]
    data["spawn_env"] = {"HUB_SPAWN_MARKER": "from-config"}
    config = HubConfig.model_validate(data)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / "T-R-030"

    handle = runner.spawn(_branched_task("T-R-030"), "claude", ws)

    assert _wait_exited(runner, handle).stdout == "from-config"


@pytest.fixture
def fake_claude_wrapped(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude-wrapped",
        "#!/bin/sh\n"
        'ID=$(grep "^id:" | tail -1 | sed "s/^id: *//")\n'
        "cat <<'EOF' | sed \"s/__ID__/$ID/\"\n"
        '{"type":"result","subtype":"success","total_cost_usd":1.0,"result":"```hub-report\\n{\\"kind\\": \\"final\\", \\"task_id\\": \\"__ID__\\", \\"result\\": \\"completed\\", \\"summary\\": \\"ok\\", \\"report_md\\": \\"done by wrapped stdout\\", \\"pr_url\\": \\"https://example/pr/9\\"}\\n```"}\n'
        "EOF\n",
    )


@pytest.fixture
def fake_claude_stream(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude-stream",
        "#!/bin/sh\n"
        'ID=$(grep "^id:" | tail -1 | sed "s/^id: *//")\n'
        "cat <<'EOF' | sed \"s/__ID__/$ID/\"\n"
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"開始處理"},{"type":"tool_use","name":"Bash","input":{"command":"echo hi"}}]}}\n'
        '{"type":"result","subtype":"success","result":"```hub-report\\n{\\"kind\\": \\"final\\", \\"task_id\\": \\"__ID__\\", \\"result\\": \\"completed\\", \\"summary\\": \\"ok\\", \\"report_md\\": \\"done by stream\\", \\"pr_url\\": \\"https://example/pr/9\\"}\\n```"}\n'
        "EOF\n",
    )


@pytest.fixture
def fake_codex_stream(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-codex-stream",
        "#!/bin/sh\n"
        'ID=$(grep "^id:" | tail -1 | sed "s/^id: *//")\n'
        "cat <<'EOF' | sed \"s/__ID__/$ID/\"\n"
        '{"type":"thread.started","thread_id":"thread-1"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"item-1","type":"agent_message","text":"```hub-report\\n{\\"kind\\": \\"final\\", \\"task_id\\": \\"__ID__\\", \\"result\\": \\"completed\\", \\"summary\\": \\"Phase 3 verified\\", \\"report_md\\": \\"Codex verified dashboard API and UI contracts\\", \\"pr_url\\": null}\\n```"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}\n'
        "EOF\n",
    )


def test_full_cycle_with_stream_jsonl_stdout(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude_stream: Path
):
    config = _runner_config(fake_claude_stream, tmp_path)
    projects = {"proj-a": _project_cfg(bare_repo)}
    runner = ClaudeRunner(runner_hub, config, projects, term_grace_seconds=1.0)
    scheduler = Scheduler(runner_hub, config, projects, runner, FakeClock())
    put_task(runner_hub, "backlog", make_task(id="T-STREAM-001"))

    scheduler.tick()
    review_path = runner_hub / "tasks" / "review" / "T-STREAM-001.md"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not review_path.exists():
        time.sleep(0.1)
        scheduler.tick()

    assert review_path.exists()
    assert "done by stream" in hubfs.read_task(review_path).report_md


def test_codex_jsonl_review_task_completes_to_done(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_codex_stream: Path
):
    config = _runner_config(fake_codex_stream, tmp_path, agent="codex")
    projects = {"proj-a": _project_cfg(bare_repo, allowed_agents=["codex"])}
    runner = ClaudeRunner(runner_hub, config, projects, term_grace_seconds=1.0)
    scheduler = Scheduler(runner_hub, config, projects, runner, FakeClock())
    put_task(
        runner_hub,
        "backlog",
        make_task(
            id="T-CODEX-REVIEW-001",
            type="review",
            assigned_to="codex",
            related_task="T-PHASE3",
        ),
    )

    scheduler.tick()
    done_path = runner_hub / "tasks" / "done" / "T-CODEX-REVIEW-001.md"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not done_path.exists():
        time.sleep(0.1)
        scheduler.tick()

    assert done_path.exists()
    completed = hubfs.read_task(done_path)
    assert completed.claimed_by == "codex"
    assert "Codex verified dashboard API and UI contracts" in completed.report_md


def test_full_cycle_with_wrapped_cli_stdout(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude_wrapped: Path
):
    config = _runner_config(fake_claude_wrapped, tmp_path)
    projects = {"proj-a": _project_cfg(bare_repo)}
    runner = ClaudeRunner(runner_hub, config, projects, term_grace_seconds=1.0)
    scheduler = Scheduler(runner_hub, config, projects, runner, FakeClock())
    put_task(runner_hub, "backlog", make_task(id="T-WRAP-001"))

    scheduler.tick()
    review_path = runner_hub / "tasks" / "review" / "T-WRAP-001.md"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not review_path.exists():
        time.sleep(0.1)
        scheduler.tick()

    assert review_path.exists()
    task = hubfs.read_task(review_path)
    assert "done by wrapped stdout" in task.report_md


@pytest.fixture
def fake_claude_limit_hit(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude-limit-hit",
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "cat <<'EOF'\n"
        '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Claude AI usage limit reached|1767225600"}\n'
        "EOF\n"
        "exit 1\n",
    )


@pytest.fixture
def fake_codex_limit_hit(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-codex-limit-hit",
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "cat <<'EOF'\n"
        '{"type":"error","message":"You have hit your usage limit. Too many requests."}\n'
        "EOF\n"
        "exit 1\n",
    )


@pytest.fixture
def fake_claude_discusses_limits(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude-discusses-limits",
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "cat <<'EOF'\n"
        '{"type":"assistant","message":{"content":[{"type":"text","text":"我們應該幫這個 API 加上 rate limit 保護"}]}}\n'
        '{"type":"result","subtype":"success","result":"done"}\n'
        "EOF\n",
    )


def _exited_result(runner_hub: Path, tmp_path: Path, bare_repo: Path, command: Path, task_id: str):
    config = _runner_config(command, tmp_path)
    runner = ClaudeRunner(runner_hub, config, {"proj-a": _project_cfg(bare_repo)}, term_grace_seconds=1.0)
    ws = Path(config.workspaces_root) / "proj-a" / "claude" / task_id
    handle = runner.spawn(_branched_task(task_id), "claude", ws)
    return _wait_exited(runner, handle)


def test_poll_flags_claude_usage_limit_and_parses_reset_epoch(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude_limit_hit: Path
):
    result = _exited_result(runner_hub, tmp_path, bare_repo, fake_claude_limit_hit, "T-RL-001")
    assert result.rate_limited is True
    assert result.rate_limit_reset_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_poll_flags_codex_error_event_without_reset_time(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_codex_limit_hit: Path
):
    result = _exited_result(runner_hub, tmp_path, bare_repo, fake_codex_limit_hit, "T-RL-002")
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is None


def test_poll_ignores_rate_limit_talk_in_conversation(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude_discusses_limits: Path
):
    result = _exited_result(runner_hub, tmp_path, bare_repo, fake_claude_discusses_limits, "T-RL-003")
    assert result.rate_limited is False
    assert result.rate_limit_reset_at is None


def test_detect_rate_limit_covers_stderr_and_plain_text_stdout():
    assert _detect_rate_limit("", "429 Too Many Requests") == (True, None)

    flagged, reset_at = _detect_rate_limit("Claude AI usage limit reached|1767225600", "")
    assert flagged is True
    assert reset_at == datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert _detect_rate_limit("all good, nothing to see", "") == (False, None)


def test_detect_rate_limit_ignores_successful_result_text():
    raw = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"已為 API 加上 usage limit reached 的錯誤處理"}\n'
    )
    assert _detect_rate_limit(raw, "") == (False, None)


@pytest.fixture
def fake_claude_limit_on_stderr(tmp_path: Path) -> Path:
    return _make_executable(
        tmp_path / "fake-claude-limit-stderr",
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "echo 'Error: 429 Too Many Requests' >&2\n"
        "exit 1\n",
    )


def test_poll_flags_rate_limit_reported_only_on_stderr(
    runner_hub: Path, tmp_path: Path, bare_repo: Path, fake_claude_limit_on_stderr: Path
):
    result = _exited_result(runner_hub, tmp_path, bare_repo, fake_claude_limit_on_stderr, "T-RL-004")
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is None


@pytest.mark.parametrize(
    "message",
    [
        "Claude AI usage limit reached",
        "You have exceeded the rate limit for this model",
        "HTTP 429: Too Many Requests",
        "Your quota exceeded for this billing period",
    ],
)
def test_every_rate_limit_phrase_is_detected(message: str):
    assert _detect_rate_limit(f'{{"type":"error","message":"{message}"}}', "")[0] is True


def test_parse_reset_epoch_requires_pipe_anchor():
    assert _parse_reset_epoch("request 1234567890 failed") is None


def test_detect_rate_limit_survives_an_unrepresentable_reset_epoch():
    assert _detect_rate_limit("usage limit reached|999999999999", "") == (True, None)


def test_detect_rate_limit_reads_nested_error_message():
    raw = '{"error":{"message":"usage limit reached|1767225600"}}\n'
    flagged, reset_at = _detect_rate_limit(raw, "")
    assert flagged is True
    assert reset_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
