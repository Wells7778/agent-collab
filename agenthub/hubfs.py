from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import fcntl

import yaml

from agenthub.schema import AgentStatus, Event, EventActor, EventType, HubConfig, ProjectConfig, TaskFile

_PR_LINE_RE = re.compile(r"^PR:\s*(\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class HubPaths:
    hub_dir: Path

    @property
    def backlog(self) -> Path:
        return self.hub_dir / "tasks" / "backlog"

    @property
    def invalid(self) -> Path:
        return self.hub_dir / "tasks" / "invalid"

    @property
    def in_progress_root(self) -> Path:
        return self.hub_dir / "tasks" / "in-progress"

    def in_progress(self, agent: str) -> Path:
        return self.in_progress_root / agent

    @property
    def blocked(self) -> Path:
        return self.hub_dir / "tasks" / "blocked"

    @property
    def review(self) -> Path:
        return self.hub_dir / "tasks" / "review"

    @property
    def done(self) -> Path:
        return self.hub_dir / "tasks" / "done"

    @property
    def events(self) -> Path:
        return self.hub_dir / "events" / "events.jsonl"

    def status(self, agent: str) -> Path:
        return self.hub_dir / "status" / f"{agent}.json"

    @property
    def messages(self) -> Path:
        return self.hub_dir / "messages"

    @property
    def config_file(self) -> Path:
        return self.hub_dir / "config.yaml"

    @property
    def projects_file(self) -> Path:
        return self.hub_dir / "projects.yaml"

    @property
    def dashboard_events(self) -> Path:
        return self.hub_dir / "events" / "dashboard.jsonl"

    @property
    def status_dir(self) -> Path:
        return self.hub_dir / "status"

    @property
    def handoffs(self) -> Path:
        return self.hub_dir / "handoffs"

    @property
    def protocol_file(self) -> Path:
        return self.hub_dir / "PROTOCOL.md"

    @property
    def tasks_root(self) -> Path:
        return self.hub_dir / "tasks"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextmanager
def directory_lock(path: Path) -> Iterator[None]:
    path.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_task(path: Path) -> TaskFile:
    return TaskFile.from_markdown(path.read_text())


def write_task(path: Path, task: TaskFile) -> None:
    atomic_write(path, task.to_markdown())


def move_task(task: TaskFile, dest_path: Path, src_path: Path) -> None:
    if src_path == dest_path:
        write_task(dest_path, task)
        return
    write_task(src_path, task)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src_path, dest_path)


def move_raw_file(src_path: Path, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src_path, dest_path)


def list_task_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".md")


def list_in_progress_agent_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def transition(task: TaskFile, src_path: Path, dest_dir: Path, update: dict) -> TaskFile:
    updated = task.model_copy(update=update)
    move_task(updated, dest_dir / f"{updated.id}.md", src_path)
    return updated


def format_pr_line(url: str) -> str:
    return f"PR: {url}"


def extract_pr_url(report_md: str) -> str | None:
    matches = _PR_LINE_RE.findall(report_md)
    return matches[-1].strip() if matches else None


def append_event(events_path: Path, event: Event) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    with open(events_path, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def emit_event(
    path: Path,
    actor: EventActor,
    event: EventType,
    ts: datetime,
    task_id: str | None = None,
    agent: str | None = None,
    detail: dict | None = None,
) -> None:
    append_event(
        path,
        Event(ts=ts, actor=actor, event=event, task_id=task_id, agent=agent, detail=detail or {}),
    )


def write_status(path: Path, status: AgentStatus) -> None:
    atomic_write(path, status.model_dump_json(indent=2))


def read_status(path: Path) -> AgentStatus | None:
    if not path.is_file():
        return None
    return AgentStatus.model_validate_json(path.read_text())


def write_message(path: Path, content: str) -> None:
    atomic_write(path, content)


def load_config(path: Path) -> HubConfig:
    data = yaml.safe_load(path.read_text())
    return HubConfig.model_validate(data)


def load_projects(path: Path) -> dict[str, ProjectConfig]:
    data = yaml.safe_load(path.read_text()) or {}
    return {name: ProjectConfig.model_validate(value) for name, value in data.items()}


def read_events(path: Path) -> list[Event]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        events.append(Event.model_validate(json.loads(line)))
    return events


def read_events_tail(path: Path, limit: int, chunk_size: int = 256 * 1024) -> list[Event]:
    if not path.is_file() or limit <= 0:
        return []
    size = path.stat().st_size
    offset = max(0, size - chunk_size)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if offset > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline != -1 else b""
    lines = [line for line in data.decode("utf-8").splitlines() if line.strip()]
    return [Event.model_validate(json.loads(line)) for line in lines[-limit:]]
