from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from agenthub import hubfs
from agenthub.schema import Event, SourceInfo, TaskFile
from agenthub.scheduler import PollResult, RunHandle, Scheduler

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict = {
    "workspaces_root": "~/agent-workspaces-test",
    "max_concurrent_global": 2,
    "max_concurrent_per_branch_base": 1,
    "max_concurrent_per_agent": 1,
    "task_timeout_minutes": 120,
    "heartbeat_seconds": 60,
    "worktree_retention_days": 7,
    "max_generation": 3,
    "agents": {
        "claude": {"enabled": True, "runtime": "claude", "skills": ["general", "rust", "ruby", "python"]},
        "codex": {"enabled": True, "runtime": "codex", "skills": ["general", "python"]},
        "grok": {"enabled": True, "runtime": "grok", "skills": ["general", "python"]},
        "hermes": {"enabled": False, "skills": ["general"]},
        "agy": {
            "enabled": True,
            "runtime": "agy",
            "skills": ["research"],
            "task_types": ["research"],
        },
    },
}

DEFAULT_PROJECTS: dict = {
    "proj-a": {
        "repo": "codecommit::us-west-2://proj-a",
        "default_branch": "develop",
        "setup": [],
        "setup_secrets": [],
        "test": [],
        "knowledge_paths": [],
        "allowed_agents": ["claude", "codex", "agy"],
    },
    "proj-b": {
        "repo": "codecommit::us-west-2://proj-b",
        "default_branch": "main",
        "setup": [],
        "setup_secrets": [],
        "test": [],
        "knowledge_paths": [],
        "allowed_agents": ["claude", "codex"],
    },
    "proj-c": {
        "repo": "codecommit::us-west-2://proj-c",
        "default_branch": "main",
        "setup": [],
        "setup_secrets": [],
        "test": [],
        "knowledge_paths": [],
        "allowed_agents": ["grok"],
    },
}


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    hub = tmp_path / "agent-hub"
    for sub in (
        "tasks/backlog",
        "tasks/invalid",
        "tasks/blocked",
        "tasks/review",
        "tasks/done",
        "tasks/in-progress/claude",
        "tasks/in-progress/codex",
        "tasks/in-progress/grok",
        "tasks/in-progress/hermes",
        "events",
        "status",
        "messages",
        "handoffs",
    ):
        (hub / sub).mkdir(parents=True, exist_ok=True)
    (hub / "config.yaml").write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))
    (hub / "projects.yaml").write_text(yaml.safe_dump(DEFAULT_PROJECTS, sort_keys=False))
    return hub


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


class FakeRunner:
    def __init__(self, clock: FakeClock):
        self._clock = clock
        self._next_pid = 1000
        self.spawn_calls: list[tuple] = []
        self.kill_calls: list[RunHandle] = []
        self.checkpoint_calls: list[RunHandle] = []
        self._poll_scripts: dict[str, list[PollResult]] = {}
        self._pid_to_task_id: dict[int, str] = {}
        self._alive_pids: set[int] = set()
        self.changed_files_by_task: dict[str, list[str]] = {}

    def script_poll(self, task_id: str, results: list[PollResult]) -> None:
        self._poll_scripts[task_id] = list(results)

    def spawn(self, task: TaskFile, agent_name: str, workspace_dir: Path) -> RunHandle:
        pid = self._next_pid
        self._next_pid += 1
        handle = RunHandle(pid=pid, pgid=pid + 5000, started_at=self._clock.now())
        self._pid_to_task_id[pid] = task.id
        self._alive_pids.add(pid)
        self.spawn_calls.append((task.id, agent_name, workspace_dir))
        return handle

    def poll(self, handle: RunHandle) -> PollResult:
        task_id = self._pid_to_task_id.get(handle.pid)
        scripts = self._poll_scripts.get(task_id, [])
        if scripts:
            result = scripts.pop(0)
            if result.exited:
                self._alive_pids.discard(handle.pid)
            return result
        return PollResult(exited=False)

    def kill_pgid(self, handle: RunHandle) -> None:
        self.kill_calls.append(handle)
        self._alive_pids.discard(handle.pid)

    def is_alive(self, pid: int, started_at: datetime) -> bool:
        return pid in self._alive_pids

    def checkpoint_workspace(self, handle: RunHandle) -> None:
        self.checkpoint_calls.append(handle)

    def changed_files(self, task: TaskFile, agent_name: str) -> list[str] | None:
        return self.changed_files_by_task.get(task.id, [])

    def mark_dead(self, pid: int) -> None:
        self._alive_pids.discard(pid)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def runner(clock: FakeClock) -> FakeRunner:
    return FakeRunner(clock)


@pytest.fixture
def scheduler(hub_dir: Path, runner: FakeRunner, clock: FakeClock) -> Scheduler:
    return Scheduler.from_hub_dir(hub_dir, runner, clock)


def make_task(**overrides) -> TaskFile:
    defaults = dict(
        id="T-20260826-001",
        type="coding",
        title="Test task",
        source=SourceInfo(type="manual"),
        project="proj-a",
        skills_required=["general"],
        priority="P2",
        depends_on=[],
        assigned_to=None,
        related_task=None,
        claimed_by=None,
        claimed_at=None,
        generation=0,
        status="backlog",
        requirement_md="Do the thing.",
        acceptance_md="- [ ] it works",
        report_md="",
    )
    defaults.update(overrides)
    return TaskFile(**defaults)


def put_task(hub_dir: Path, subdir: str, task: TaskFile) -> Path:
    path = hub_dir / "tasks" / subdir / f"{task.id}.md"
    hubfs.write_task(path, task)
    return path


def write_config(hub_dir: Path, **overrides) -> None:
    config = {**DEFAULT_CONFIG, **overrides}
    (hub_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))


def write_projects(hub_dir: Path, projects: dict) -> None:
    (hub_dir / "projects.yaml").write_text(yaml.safe_dump(projects, sort_keys=False))


def read_events(hub_dir: Path) -> list[Event]:
    return hubfs.read_events(hub_dir / "events" / "events.jsonl")


def read_messages(hub_dir: Path, agent: str = "claude") -> list[Path]:
    return sorted((hub_dir / "messages").glob(f"*-{agent}-human.md"))


def hub_report_block(payload: dict) -> str:
    return "```hub-report\n" + json.dumps(payload) + "\n```"


def dispatch_task(scheduler: Scheduler, hub_dir: Path, task_id: str, **overrides) -> Path:
    task = make_task(id=task_id, **overrides)
    put_task(hub_dir, "backlog", task)
    scheduler.tick()
    return hub_dir / "tasks" / "in-progress" / "claude" / f"{task_id}.md"
