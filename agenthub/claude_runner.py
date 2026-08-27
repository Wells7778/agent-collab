from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from agenthub import hubfs, provision
from agenthub.schema import HubConfig, ProjectConfig, TaskFile
from agenthub.scheduler import PollResult, RunHandle


@dataclass
class _LiveProcess:
    popen: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    workspace_dir: Path
    delivered: bool = False


class ClaudeRunner:
    def __init__(
        self,
        hub_dir: Path,
        config: HubConfig,
        projects: dict[str, ProjectConfig],
        term_grace_seconds: float = 10.0,
    ) -> None:
        self.hub_dir = hub_dir
        self.paths = hubfs.HubPaths(hub_dir)
        self.config = config
        self.projects = projects
        self.term_grace_seconds = term_grace_seconds
        self._live: dict[int, _LiveProcess] = {}

    def spawn(self, task: TaskFile, agent_name: str, workspace_dir: Path) -> RunHandle:
        self._live = {pid: live for pid, live in self._live.items() if not live.delivered}
        project_cfg = self.projects[task.project]
        knowledge_dirs = provision.sync_knowledge(self.hub_dir, task.project, project_cfg)
        provision.provision_workspace(
            Path(self.config.workspaces_root).expanduser(), workspace_dir, task, project_cfg
        )
        run_dir = workspace_dir.parent / f"{workspace_dir.name}.hub"
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = run_dir / f"prompt-g{task.generation}.md"
        prompt_path.write_text(self._build_prompt(task, knowledge_dirs))
        stdout_path = run_dir / f"stdout-g{task.generation}.log"
        stderr_path = run_dir / f"stderr-g{task.generation}.log"
        command = self.config.agents[agent_name].command
        with open(prompt_path) as stdin, open(stdout_path, "w") as stdout, open(stderr_path, "w") as stderr:
            popen = subprocess.Popen(
                command,
                cwd=workspace_dir,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                env={**os.environ, **self.config.spawn_env},
            )
        handle = RunHandle(
            pid=popen.pid, pgid=os.getpgid(popen.pid), started_at=datetime.now(timezone.utc)
        )
        self._live[popen.pid] = _LiveProcess(
            popen=popen, stdout_path=stdout_path, stderr_path=stderr_path, workspace_dir=workspace_dir
        )
        return handle

    def poll(self, handle: RunHandle) -> PollResult:
        live = self._live.get(handle.pid)
        if live is None:
            if self.is_alive(handle.pid, handle.started_at):
                return PollResult(exited=False)
            return PollResult(exited=True, stdout="")
        if live.popen.poll() is None:
            return PollResult(exited=False)
        live.delivered = True
        raw = live.stdout_path.read_text()
        rate_limited, reset_at = _detect_rate_limit(raw, live.stderr_path.read_text())
        return PollResult(
            exited=True,
            stdout=_agent_text(raw),
            rate_limited=rate_limited,
            rate_limit_reset_at=reset_at,
        )

    def kill_pgid(self, handle: RunHandle) -> None:
        if not self._signal_group(handle.pgid, signal.SIGTERM):
            self._reap(handle)
            return
        if self._await_group_exit(handle):
            return
        self._signal_group(handle.pgid, signal.SIGKILL)
        self._await_group_exit(handle)

    def _await_group_exit(self, handle: RunHandle) -> bool:
        deadline = time.monotonic() + self.term_grace_seconds
        while time.monotonic() < deadline:
            self._reap(handle)
            if not self._group_alive(handle.pgid):
                return True
            time.sleep(0.05)
        return False

    def is_alive(self, pid: int, started_at: datetime) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        reported = _process_start_time(pid)
        if reported is None:
            return False
        return abs((reported - started_at).total_seconds()) <= 2

    def checkpoint_workspace(self, handle: RunHandle) -> None:
        live = self._live.get(handle.pid)
        if live is None:
            return
        workspace = live.workspace_dir
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True
        )
        if status.returncode != 0 or not status.stdout.strip():
            return
        subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=agent-hub",
                "-c",
                "user.email=daemon@agent-hub.local",
                "commit",
                "-m",
                f"checkpoint: recovered WIP for {live.workspace_dir.name}",
            ],
            cwd=workspace,
            capture_output=True,
        )

    def _build_prompt(self, task: TaskFile, knowledge_dirs: list[Path]) -> str:
        parts = [
            self.paths.protocol_file.read_text(),
            "# 任務檔\n\n" + task.to_markdown(),
        ]
        handoff = self._latest_handoff(task.id)
        if handoff is not None:
            parts.append("# 最新 handoff\n\n" + handoff.read_text())
        if knowledge_dirs:
            listing = "\n".join(f"- {d}" for d in knowledge_dirs)
            parts.append("# knowledge projection(唯讀)\n\n" + listing)
        return "\n\n---\n\n".join(parts)

    def _latest_handoff(self, task_id: str) -> Path | None:
        candidates = sorted(self.paths.handoffs.glob(f"{task_id}.handoff-*.md"), key=_handoff_number)
        return candidates[-1] if candidates else None

    def _signal_group(self, pgid: int, sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False

    def _group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _reap(self, handle: RunHandle) -> None:
        live = self._live.get(handle.pid)
        if live is not None:
            live.popen.poll()


_RATE_LIMIT_RE = re.compile(
    r"usage limit reached|rate limit|too many requests|quota exceeded", re.IGNORECASE
)
_RESET_EPOCH_RE = re.compile(r"\|(\d{9,12})\b")


def _detect_rate_limit(stdout_raw: str, stderr_text: str) -> tuple[bool, datetime | None]:
    candidates = _error_texts(stdout_raw)
    stripped = stdout_raw.strip()
    if stripped and not stripped.startswith("{"):
        candidates.append(stdout_raw)
    if stderr_text.strip():
        candidates.append(stderr_text)
    for text in candidates:
        if _RATE_LIMIT_RE.search(text):
            return True, _parse_reset_epoch(text)
    return False, None


def _parse_reset_epoch(text: str) -> datetime | None:
    match = _RESET_EPOCH_RE.search(text)
    if match is None:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)


def _error_texts(raw: str) -> list[str]:
    texts: list[str] = []
    for data in _iter_json_lines(raw):
        if data.get("is_error") is True and isinstance(data.get("result"), str):
            texts.append(data["result"])
        if data.get("type") == "error" and isinstance(data.get("message"), str):
            texts.append(data["message"])
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            texts.append(error["message"])
    return texts


def _iter_json_lines(raw: str) -> Iterator[dict]:
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _agent_text(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return raw
    assistant_texts: list[str] = []
    result_text: str | None = None
    parsed_any = False
    for data in _iter_json_lines(stripped):
        parsed_any = True
        if isinstance(data.get("result"), str):
            result_text = data["result"]
        item = data.get("item")
        if (
            data.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            assistant_texts.append(item["text"])
        for block in _message_content(data):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                assistant_texts.append(block["text"])
    if not parsed_any:
        return raw
    if result_text is not None and assistant_texts and assistant_texts[-1] == result_text:
        assistant_texts.pop()
    parts = assistant_texts + ([result_text] if result_text is not None else [])
    return "\n\n".join(parts) if parts else raw


def _message_content(data: dict) -> list:
    if data.get("type") != "assistant":
        return []
    message = data.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _handoff_number(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-", 1)[-1])
    except ValueError:
        return 0


def _process_start_time(pid: int) -> datetime | None:
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        parsed = datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.astimezone()
