from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, Protocol

from agenthub import hubfs
from agenthub.schema import (
    AgentStatus,
    HubConfig,
    HubReport,
    ProjectConfig,
    SourceInfo,
    TaskFile,
    TaskFileParseError,
    WorkspaceInfo,
    parse_frontmatter_lenient,
)

_HUB_REPORT_BLOCK_RE = re.compile(r"```hub-report\s*\n(.*?)\n```", re.DOTALL)
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_NON_WRITING_TASK_TYPES = {"review"}


@dataclass(frozen=True)
class RunHandle:
    pid: int
    pgid: int
    started_at: datetime


@dataclass(frozen=True)
class PollResult:
    exited: bool
    stdout: str = ""
    rate_limited: bool = False
    rate_limit_reset_at: datetime | None = None


class AgentRunner(Protocol):
    def spawn(self, task: TaskFile, agent_name: str, workspace_dir: Path) -> RunHandle: ...

    def poll(self, handle: RunHandle) -> PollResult: ...

    def kill_pgid(self, handle: RunHandle) -> None: ...

    def is_alive(self, pid: int, started_at: datetime) -> bool: ...

    def checkpoint_workspace(self, handle: RunHandle) -> None: ...

    def changed_files(self, task: TaskFile, agent_name: str) -> list[str] | None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass
class _RunningTask:
    task_id: str
    agent: str
    project: str
    handle: RunHandle

    def to_status(self, now: datetime) -> AgentStatus:
        return AgentStatus(
            agent=self.agent,
            state="working",
            task_id=self.task_id,
            project=self.project,
            phase="running",
            pid=self.handle.pid,
            pgid=self.handle.pgid,
            started_at=self.handle.started_at,
            heartbeat_at=now,
        )


_InProgressEntry = tuple[str, Path, TaskFile]


class Scheduler:
    def __init__(
        self,
        hub_dir: Path,
        config: HubConfig,
        projects: dict[str, ProjectConfig],
        runner: AgentRunner,
        clock: Clock,
    ) -> None:
        self.hub_dir = hub_dir
        self.paths = hubfs.HubPaths(hub_dir)
        self.config = config
        self.projects = projects
        self.runner = runner
        self.clock = clock
        self._running: dict[str, _RunningTask] = {}
        self._cooldowns: dict[str, datetime] = {}

    @classmethod
    def from_hub_dir(cls, hub_dir: Path, runner: AgentRunner, clock: Clock) -> "Scheduler":
        paths = hubfs.HubPaths(hub_dir)
        config = hubfs.load_config(paths.config_file)
        projects = hubfs.load_projects(paths.projects_file)
        return cls(hub_dir, config, projects, runner, clock)

    def tick(self) -> None:
        in_progress = list(self._iter_in_progress())
        dispatched = self._dispatch_backlog(in_progress)
        self._poll_in_progress(in_progress + dispatched)
        self._heartbeat()

    def startup_scan(self) -> None:
        self._restore_cooldowns()
        statuses: dict[str, AgentStatus | None] = {}
        for agent, task_path, task in self._iter_in_progress():
            if agent not in statuses:
                statuses[agent] = hubfs.read_status(self.paths.status(agent))
            status = statuses[agent]

            handle: RunHandle | None = None
            alive = False
            if (
                status is not None
                and status.task_id == task.id
                and status.pid is not None
                and status.pgid is not None
                and status.started_at is not None
            ):
                handle = RunHandle(pid=status.pid, pgid=status.pgid, started_at=status.started_at)
                alive = self.runner.is_alive(status.pid, status.started_at)

            if alive and handle is not None:
                self._running[task.id] = _RunningTask(
                    task_id=task.id, agent=agent, project=task.project, handle=handle
                )
            elif not alive:
                self._recover(task, task_path, agent, handle, is_timeout=False)

    def _restore_cooldowns(self) -> None:
        now = self.clock.now()
        for agent in self.config.agents:
            status = hubfs.read_status(self.paths.status(agent))
            if status is not None and status.cooldown_until is not None and status.cooldown_until > now:
                self._cooldowns[agent] = status.cooldown_until

    def _iter_in_progress(self) -> Iterator[_InProgressEntry]:
        for agent_dir in hubfs.list_in_progress_agent_dirs(self.paths.in_progress_root):
            for task_path in hubfs.list_task_files(agent_dir):
                try:
                    task = hubfs.read_task(task_path)
                except TaskFileParseError:
                    continue
                yield agent_dir.name, task_path, task

    def _dispatch_backlog(self, in_progress: list[_InProgressEntry]) -> list[_InProgressEntry]:
        candidates = self._scan_and_validate_backlog()
        candidates.sort(key=lambda t: (_PRIORITY_RANK[t.priority], t.id))

        global_count = len(in_progress)
        write_slot_counts = Counter(
            slot for _, _, task in in_progress if (slot := self._write_slot(task)) is not None
        )
        agent_counts = Counter(agent for agent, _, _ in in_progress)

        dispatched: list[_InProgressEntry] = []
        for task in candidates:
            if global_count >= self.config.max_concurrent_global:
                break
            write_slot = self._write_slot(task)
            if (
                write_slot is not None
                and write_slot_counts[write_slot] >= self.config.max_concurrent_per_branch_base
            ):
                continue
            if not self._dependencies_satisfied(task):
                continue

            agent = self._route(task, agent_counts)
            if agent is None:
                continue

            entry = self._dispatch(task, agent)
            if entry is None:
                continue
            dispatched.append(entry)
            global_count += 1
            if write_slot is not None:
                write_slot_counts[write_slot] += 1
            agent_counts[agent] += 1
        return dispatched

    def _write_slot(self, task: TaskFile) -> tuple[str, str] | None:
        if task.type in _NON_WRITING_TASK_TYPES:
            return None
        project_cfg = self.projects.get(task.project)
        if project_cfg is None:
            return None
        return task.project, project_cfg.default_branch

    def _scan_and_validate_backlog(self) -> list[TaskFile]:
        valid: list[TaskFile] = []
        for path in hubfs.list_task_files(self.paths.backlog):
            raw = path.read_text()
            try:
                task = TaskFile.from_markdown(raw)
            except TaskFileParseError as exc:
                self._quarantine_invalid(path, raw, str(exc))
                continue
            valid.append(task)
        return valid

    def _quarantine_invalid(self, path: Path, raw: str, error: str) -> None:
        loaded = parse_frontmatter_lenient(raw)
        task_id = loaded["id"] if loaded is not None and isinstance(loaded.get("id"), str) else path.stem
        hubfs.move_raw_file(path, self.paths.invalid / path.name)
        self._emit("task_invalid", task_id, None, {"error": error, "file": path.name})

    def _dependencies_satisfied(self, task: TaskFile) -> bool:
        for dep_id in task.depends_on:
            dep_path = self.paths.done / f"{dep_id}.md"
            if not dep_path.is_file():
                return False
            fields = parse_frontmatter_lenient(dep_path.read_text())
            if fields is None or fields.get("status") == "cancelled":
                return False
        return True

    def _route(self, task: TaskFile, agent_counts: Counter[str]) -> str | None:
        max_per_agent = self.config.max_concurrent_per_agent
        if task.assigned_to:
            agent = task.assigned_to
            agent_cfg = self.config.agents.get(agent)
            if agent_cfg is None or not agent_cfg.enabled:
                return None
            if task.type not in agent_cfg.task_types:
                return None
            if not self._agent_available(agent):
                return None
            if agent_counts[agent] >= max_per_agent:
                return None
            return agent

        project_cfg = self.projects.get(task.project)
        allowed = set(project_cfg.allowed_agents) if project_cfg else set()
        required_skills = set(task.skills_required)
        for agent, agent_cfg in self.config.agents.items():
            if not agent_cfg.enabled:
                continue
            if agent not in allowed:
                continue
            if task.type not in agent_cfg.task_types:
                continue
            if not self._agent_available(agent):
                continue
            if not required_skills.issubset(set(agent_cfg.skills)):
                continue
            if agent_counts[agent] >= max_per_agent:
                continue
            return agent
        return None

    def _agent_available(self, agent: str) -> bool:
        cooldown_until = self._cooldowns.get(agent)
        if cooldown_until is None:
            return True
        if cooldown_until <= self.clock.now():
            del self._cooldowns[agent]
            return True
        return False

    def _dispatch(self, task: TaskFile, agent: str) -> _InProgressEntry | None:
        project_cfg = self.projects[task.project]
        now = self.clock.now()
        branch = f"agent/{agent}/{task.id}-g{task.generation}"
        src_path = self.paths.backlog / f"{task.id}.md"
        dispatched = hubfs.transition(
            task,
            src_path,
            self.paths.in_progress(agent),
            {
                "workspace": WorkspaceInfo(
                    repo=project_cfg.repo,
                    branch_base=project_cfg.default_branch,
                    branch=branch,
                ),
                "claimed_by": agent,
                "claimed_at": now,
                "status": "in-progress",
            },
        )
        dest_path = self.paths.in_progress(agent) / f"{task.id}.md"

        workspace_dir = Path(self.config.workspaces_root).expanduser() / task.project / agent / task.id
        try:
            handle = self.runner.spawn(dispatched, agent, workspace_dir)
        except Exception as exc:
            self._emit("spawn_failed", task.id, agent, {"error": str(exc)})
            self._recover(dispatched, dest_path, agent, None, is_timeout=False)
            return None
        running = _RunningTask(task_id=task.id, agent=agent, project=task.project, handle=handle)
        self._running[task.id] = running

        self._emit("task_dispatched", task.id, agent, {"branch": branch, "project": task.project})
        self._emit("agent_spawned", task.id, agent, {"pid": handle.pid, "pgid": handle.pgid})
        hubfs.write_status(self.paths.status(agent), running.to_status(now))
        return agent, dest_path, dispatched

    def _poll_in_progress(self, entries: list[_InProgressEntry]) -> None:
        for agent, task_path, task in entries:
            running = self._running.get(task.id)
            if running is None:
                continue
            self._poll_one(task, task_path, agent, running.handle)

    def _poll_one(self, task: TaskFile, task_path: Path, agent: str, handle: RunHandle) -> None:
        result = self.runner.poll(handle)
        if not result.exited:
            if task.claimed_at is not None and self.clock.now() - task.claimed_at > timedelta(
                minutes=self.config.task_timeout_minutes
            ):
                self._recover(task, task_path, agent, handle, is_timeout=True)
            return

        reports, parse_errors = _extract_hub_reports(result.stdout)
        for error in parse_errors:
            self._emit("report_parse_failed", task.id, agent, {"error": error})

        conclusion: HubReport | None = None
        for report in reports:
            if report.kind == "checkpoint":
                task = self._append_checkpoint(task, task_path, report)
            conclusion = report

        if conclusion is not None and conclusion.kind == "final":
            self._handle_final(task, task_path, agent, conclusion)
        elif conclusion is not None and conclusion.kind == "blocked":
            self._handle_blocked(task, task_path, agent, conclusion)
        elif result.rate_limited:
            self._handle_rate_limited(task, task_path, agent, result.rate_limit_reset_at)
        else:
            self._emit(
                "task_no_report",
                task.id,
                agent,
                {
                    "report_blocks": len(reports) + len(parse_errors),
                    "parse_errors": len(parse_errors),
                    "stdout_bytes": len(result.stdout),
                },
            )
            self._recover(task, task_path, agent, handle, is_timeout=False)

    def _append_checkpoint(self, task: TaskFile, task_path: Path, report: HubReport) -> TaskFile:
        entry = f"### Checkpoint {self.clock.now().isoformat()} — {report.summary}\n\n{report.report_md}\n"
        updated = task.with_report_appended(entry)
        hubfs.write_task(task_path, updated)
        self._emit("task_checkpoint", task.id, task.claimed_by, {"summary": report.summary})
        return updated

    def _handle_final(self, task: TaskFile, task_path: Path, agent: str, report: HubReport) -> None:
        entry = f"### Final ({report.result}) {self.clock.now().isoformat()} — {report.summary}\n\n{report.report_md}"
        if report.pr_url:
            entry += f"\n\n{hubfs.format_pr_line(report.pr_url)}"
        entry += "\n"
        finished = task.with_report_appended(entry)

        if report.result == "completed" and task.type == "review":
            dest_dir, dest_status, event = self.paths.done, "done", "task_done"
        else:
            dest_dir, dest_status, event = self.paths.review, "review", "task_review_ready"

        hubfs.transition(finished, task_path, dest_dir, {"status": dest_status})

        self._emit(event, task.id, agent, {"result": report.result})
        if report.result == "completed":
            if task.type == "review":
                self._append_review_report_to_related_task(task, agent, report)
            else:
                self._create_review_pair(finished, agent)
        self._finish_running(task.id)

    def _create_review_pair(self, task: TaskFile, agent: str) -> None:
        project_cfg = self.projects.get(task.project)
        if project_cfg is None or not project_cfg.spec_paths:
            return
        changed = self.runner.changed_files(task, agent)
        if changed is None:
            self._emit(
                "review_pair_unavailable",
                task.id,
                agent,
                {"eligible": [], "reason": "could not determine which files changed"},
            )
            return
        if not any(
            fnmatch(path, pattern) for path in changed for pattern in project_cfg.spec_paths
        ):
            return

        reviewers = self._eligible_reviewers(agent, project_cfg)
        if len(reviewers) < 2:
            self._emit(
                "review_pair_unavailable",
                task.id,
                agent,
                {"eligible": reviewers, "reason": "fewer than two reviewers with a distinct runtime"},
            )
            return

        for reviewer in reviewers[:2]:
            self._create_review_task(task, reviewer)

    def _eligible_reviewers(self, agent: str, project_cfg: ProjectConfig) -> list[str]:
        author_cfg = self.config.agents.get(agent)
        author_runtime = author_cfg.runtime if author_cfg is not None else None
        return [
            name
            for name in project_cfg.allowed_agents
            if name != agent
            and (cfg := self.config.agents.get(name)) is not None
            and cfg.enabled
            and "review" in cfg.task_types
            and (author_runtime is None or cfg.runtime != author_runtime)
        ]

    def _create_review_task(self, task: TaskFile, reviewer: str) -> None:
        review_id = f"{task.id}-review-{reviewer}"
        branch = task.workspace.branch or "(未知分支)"
        pr_url = hubfs.extract_pr_url(task.report_md) or "(無 PR,見分支)"
        review_task = TaskFile(
            id=review_id,
            type="review",
            title=f"互審({reviewer}):{task.title}",
            source=SourceInfo(type="manual"),
            project=task.project,
            skills_required=task.skills_required,
            priority=task.priority,
            assigned_to=reviewer,
            related_task=task.id,
            requirement_md=f"審查 {task.id} 的變更。\n\n分支:{branch}\nPR:{pr_url}",
            acceptance_md="- [ ] 審查報告已寫回 final 回報的 report_md(不開 PR、不留 PR comment)",
        )
        hubfs.write_task(self.paths.backlog / f"{review_id}.md", review_task)
        self._emit("review_task_created", review_id, reviewer, {"related_task": task.id})

    def _append_review_report_to_related_task(
        self, task: TaskFile, agent: str, report: HubReport
    ) -> None:
        if task.related_task is None:
            return
        related_path = self.paths.review / f"{task.related_task}.md"
        if not related_path.is_file():
            return
        entry = (
            f"### 互審回報({agent}) {self.clock.now().isoformat()} — {report.summary}\n\n"
            f"{report.report_md}\n"
        )
        hubfs.write_task(related_path, hubfs.read_task(related_path).with_report_appended(entry))

    def _handle_blocked(self, task: TaskFile, task_path: Path, agent: str, report: HubReport) -> None:
        content = f"# 提問:{task.id} ({agent})\n\n{report.question}\n"
        self._move_to_blocked(task, task_path, agent, content, {})
        self._finish_running(task.id)

    def _handle_rate_limited(
        self, task: TaskFile, task_path: Path, agent: str, reset_at: datetime | None
    ) -> None:
        now = self.clock.now()
        if reset_at is not None and reset_at > now:
            cooldown_until = reset_at
        else:
            cooldown_until = now + timedelta(minutes=self.config.rate_limit_cooldown_minutes)
        self._cooldowns[agent] = cooldown_until
        self._requeue_to_backlog(task, task_path, {})
        self._emit(
            "agent_rate_limited",
            task.id,
            agent,
            {"cooldown_until": cooldown_until.isoformat()},
        )
        self._emit("task_requeued", task.id, agent, {"generation": task.generation})
        self._finish_running(task.id)

    def _requeue_to_backlog(self, task: TaskFile, task_path: Path, update: dict) -> None:
        hubfs.transition(
            task,
            task_path,
            self.paths.backlog,
            {
                "claimed_by": None,
                "claimed_at": None,
                "workspace": task.workspace.model_copy(update={"branch": None}),
                "status": "backlog",
                **update,
            },
        )

    def _recover(
        self,
        task: TaskFile,
        task_path: Path,
        agent: str,
        handle: RunHandle | None,
        is_timeout: bool,
    ) -> None:
        if handle is not None:
            if is_timeout:
                self.runner.kill_pgid(handle)
            self.runner.checkpoint_workspace(handle)

        new_generation = task.generation + 1

        if new_generation > self.config.max_generation:
            content = (
                f"# 系統訊息:{task.id} 已達最大重試代數\n\n"
                f"任務 {task.id} 已回收 {new_generation} 次,達到 max_generation "
                f"({self.config.max_generation}),已停止自動重派,請人工處理。\n"
            )
            bumped = task.model_copy(update={"generation": new_generation})
            self._move_to_blocked(bumped, task_path, agent, content, {"reason": "max_generation_exceeded"})
        else:
            self._requeue_to_backlog(task, task_path, {"generation": new_generation})

            event = "agent_timeout" if is_timeout else "agent_exited"
            self._emit(event, task.id, agent, {"generation": new_generation})
            self._emit("task_requeued", task.id, agent, {"generation": new_generation})

        self._finish_running(task.id)

    def _move_to_blocked(
        self, task: TaskFile, task_path: Path, agent: str, message_content: str, detail: dict
    ) -> None:
        message_path = self._write_human_message(agent, message_content)
        hubfs.transition(task, task_path, self.paths.blocked, {"status": "blocked"})
        self._emit("task_blocked", task.id, agent, {**detail, "message": message_path.name})

    def _write_human_message(self, agent: str, content: str) -> Path:
        path = self.paths.messages / f"{self.clock.now().isoformat()}-{agent}-human.md"
        hubfs.write_message(path, content)
        return path

    def _finish_running(self, task_id: str) -> None:
        self._running.pop(task_id, None)

    def _heartbeat(self) -> None:
        now = self.clock.now()
        working_by_agent = {rt.agent: rt for rt in self._running.values()}
        for agent, agent_cfg in self.config.agents.items():
            if not agent_cfg.enabled:
                status = AgentStatus(agent=agent, state="offline", heartbeat_at=now)
            elif (running := working_by_agent.get(agent)) is not None:
                status = running.to_status(now)
            elif (cooldown_until := self._cooldowns.get(agent)) is not None and cooldown_until > now:
                status = AgentStatus(
                    agent=agent, state="resting", cooldown_until=cooldown_until, heartbeat_at=now
                )
            else:
                status = AgentStatus(agent=agent, state="idle", heartbeat_at=now)
            hubfs.write_status(self.paths.status(agent), status)

    def _emit(self, event: str, task_id: str | None, agent: str | None, detail: dict | None = None) -> None:
        hubfs.emit_event(
            self.paths.events, "daemon", event, self.clock.now(), task_id=task_id, agent=agent, detail=detail
        )


def _extract_hub_reports(stdout: str) -> tuple[list[HubReport], list[str]]:
    reports: list[HubReport] = []
    errors: list[str] = []
    for match in _HUB_REPORT_BLOCK_RE.finditer(stdout):
        block = match.group(1)
        try:
            data = json.loads(block, strict=False)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid json: {exc}; block={_truncate(block)}")
            continue
        try:
            reports.append(HubReport.model_validate(data))
        except Exception as exc:
            errors.append(f"schema validation failed: {exc}; block={_truncate(block)}")
    return reports, errors


def _truncate(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"
