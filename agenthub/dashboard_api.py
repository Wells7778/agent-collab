from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Iterator, Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agenthub import hubfs
from agenthub.hubfs import HubPaths
from agenthub.schema import Priority, SourceInfo, TaskFile, TaskFileParseError, WorkspaceInfo

_DASHBOARD_HTML = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
_TASK_ID_RE = re.compile(r"^T-(\d{8})-(\d{3})$")
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyText
    project: str
    type: Literal["coding", "research", "explore"] = "coding"
    requirement_md: NonEmptyText
    acceptance_md: NonEmptyText
    skills_required: list[str] = Field(default_factory=list)
    priority: Priority
    asana_url: str | None = None


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_md: NonEmptyText


class ReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_md: NonEmptyText
    assigned_to: str | None = None


class ReviewDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_to: NonEmptyText | None = None


def create_app(hub_dir: Path) -> FastAPI:
    paths = HubPaths(hub_dir)
    app = FastAPI(title="Agent Hub Dashboard")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_DASHBOARD_HTML)

    @app.get("/api/tasks")
    def list_tasks(project: str | None = None) -> dict:
        board: dict[str, list[dict]] = {
            "backlog": [],
            "in_progress": [],
            "blocked": [],
            "review": [],
            "done": [],
        }
        invalid_count = 0
        for section, path in _iter_board_files(paths):
            try:
                task = hubfs.read_task(path)
            except TaskFileParseError:
                invalid_count += 1
                continue
            if project is not None and task.project != project:
                continue
            board[section].append(_task_summary(task))
        return {**board, "invalid_count": invalid_count}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        path = _find_task_path(paths, task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        task = _read_task_or_500(path)
        return task.model_dump(mode="json")

    @app.get("/api/status")
    def list_status() -> list[dict]:
        status_dir = paths.status_dir
        if not status_dir.is_dir():
            return []
        config = hubfs.load_config(paths.config_file)
        result = []
        for path in sorted(status_dir.glob("*.json")):
            status = hubfs.read_status(path)
            if status is None:
                continue
            agent_config = config.agents.get(status.agent)
            result.append(
                {
                    **status.model_dump(mode="json"),
                    "runtime": agent_config.runtime if agent_config is not None else None,
                    "model": _model_on_command(agent_config.command) if agent_config else None,
                }
            )
        return result

    @app.get("/api/events")
    def list_events(limit: int = 100) -> list[dict]:
        events = hubfs.read_events_tail(paths.events, limit) + hubfs.read_events_tail(
            paths.dashboard_events, limit
        )
        events.sort(key=lambda e: e.ts)
        tail = events[-limit:] if limit > 0 else []
        return [e.model_dump(mode="json") for e in tail]

    @app.get("/api/messages")
    def list_messages(limit: int = 50) -> list[dict]:
        messages_dir = paths.messages
        if not messages_dir.is_dir():
            return []
        files = sorted(messages_dir.glob("*.md"))
        tail = files[-limit:] if limit > 0 else []
        return [{"filename": path.name, "content": path.read_text()} for path in tail]

    @app.get("/api/sessions/{task_id}/log")
    def session_log(task_id: str, limit: int = 80) -> dict:
        path = _find_task_path(paths, task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        task = _read_task_or_500(path)
        empty = {"entries": [], "generation": task.generation, "log_bytes": 0}
        if not task.claimed_by:
            return empty
        config = hubfs.load_config(paths.config_file)
        run_dir = (
            Path(config.workspaces_root).expanduser()
            / task.project
            / task.claimed_by
            / f"{task.id}.hub"
        )
        log_path = run_dir / f"stdout-g{task.generation}.log"
        if not log_path.is_file():
            return empty
        raw = log_path.read_text(errors="replace")
        entries = _parse_session_log(raw)
        return {"entries": entries[-limit:], "generation": task.generation, "log_bytes": len(raw)}

    @app.get("/api/projects")
    def list_projects() -> dict:
        projects = hubfs.load_projects(paths.projects_file)
        return {"projects": sorted(projects.keys())}

    @app.post("/api/tasks", status_code=201)
    def create_task(req: CreateTaskRequest) -> dict:
        projects = hubfs.load_projects(paths.projects_file)
        if req.project not in projects:
            raise HTTPException(status_code=400, detail=f"unknown project: {req.project}")

        with hubfs.directory_lock(paths.tasks_root):
            now = datetime.now(timezone.utc)
            task_id = _generate_task_id(paths, now)
            task = TaskFile(
                id=task_id,
                type=req.type,
                title=req.title,
                source=SourceInfo(type="asana" if req.asana_url else "manual", asana_url=req.asana_url),
                project=req.project,
                workspace=WorkspaceInfo(),
                skills_required=req.skills_required,
                priority=req.priority,
                depends_on=[],
                assigned_to=None,
                related_task=None,
                claimed_by=None,
                claimed_at=None,
                generation=0,
                status="backlog",
                requirement_md=req.requirement_md,
                acceptance_md=req.acceptance_md,
                report_md="",
            )
            hubfs.write_task(paths.backlog / f"{task_id}.md", task)
        _emit_dashboard_event(paths, "task_created", task_id, None, {"project": req.project})
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/reply")
    def reply_task(task_id: str, req: ReplyRequest) -> dict:
        path, task, _ = _require_task_in(paths, task_id, [("blocked", paths.blocked)])
        now = datetime.now(timezone.utc)
        entry = f"### 人類回覆 {now.isoformat()}\n\n{req.reply_md}\n"
        reported = task.with_report_appended(entry)
        updated = hubfs.transition(
            reported,
            path,
            paths.backlog,
            {"status": "backlog", "claimed_by": None, "claimed_at": None, "generation": 0},
        )
        _emit_dashboard_event(paths, "task_replied", task_id, task.claimed_by)
        return updated.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/complete")
    def complete_task(task_id: str) -> dict:
        path, task, _ = _require_task_in(paths, task_id, [("review", paths.review)])
        updated = hubfs.transition(task, path, paths.done, {"status": "done"})
        _emit_dashboard_event(paths, "task_completed", task_id, task.claimed_by)
        return updated.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/return")
    def return_task(task_id: str, req: ReturnRequest) -> dict:
        path, task, _ = _require_task_in(paths, task_id, [("review", paths.review)])
        now = datetime.now(timezone.utc)
        entry = f"### 打回意見 {now.isoformat()}\n\n{req.feedback_md}\n"
        new_assigned_to = req.assigned_to if req.assigned_to is not None else task.claimed_by
        reported = task.with_report_appended(entry)
        updated = hubfs.transition(
            reported,
            path,
            paths.backlog,
            {
                "status": "backlog",
                "claimed_by": None,
                "claimed_at": None,
                "assigned_to": new_assigned_to,
                "generation": 0,
            },
        )
        _emit_dashboard_event(
            paths, "task_returned", task_id, new_assigned_to, {"previous_agent": task.claimed_by}
        )
        return updated.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict:
        path, task, source = _require_task_in(
            paths, task_id, [("review", paths.review), ("blocked", paths.blocked)]
        )
        updated = hubfs.transition(task, path, paths.done, {"status": "cancelled"})
        _emit_dashboard_event(paths, "task_cancelled", task_id, task.claimed_by, {"from": source})
        return updated.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/review")
    def dispatch_review(
        task_id: str, req: ReviewDispatchRequest | None = Body(default=None)
    ) -> dict:
        _, task, _ = _require_task_in(paths, task_id, [("review", paths.review)])
        config = hubfs.load_config(paths.config_file)
        author_runtime = _runtime_of(config, task.claimed_by)
        assigned_to = req.assigned_to if req is not None else None
        if assigned_to is not None and not _is_independent_reviewer(
            config, assigned_to, task.claimed_by, author_runtime
        ):
            conflict = "runtime" if assigned_to != task.claimed_by else "role"
            raise HTTPException(
                status_code=409,
                detail=f"review must not share the {conflict} of {task.claimed_by}",
            )
        if assigned_to is None and task.claimed_by is not None:
            project = hubfs.load_projects(paths.projects_file).get(task.project)
            required_skills = set(task.skills_required)
            candidates = project.allowed_agents if project is not None else []
            assigned_to = next(
                (
                    agent
                    for agent in candidates
                    if _is_independent_reviewer(config, agent, task.claimed_by, author_runtime)
                    and (agent_config := config.agents.get(agent)) is not None
                    and agent_config.enabled
                    and required_skills.issubset(set(agent_config.skills))
                ),
                None,
            )
            if assigned_to is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"no eligible reviewer is available for task {task.id}",
                )

        branch = task.workspace.branch or "(未知分支)"
        pr_url = hubfs.extract_pr_url(task.report_md)
        pr_line = pr_url if pr_url else "(無 PR,見分支)"
        requirement_md = (
            f"互審任務:審查 {task.id} 的變更。\n\n分支:{branch}\nPR:{pr_line}"
        )
        with hubfs.directory_lock(paths.tasks_root):
            now = datetime.now(timezone.utc)
            review_id = _generate_task_id(paths, now)
            review_task = TaskFile(
                id=review_id,
                type="review",
                title=f"互審:{task.title}",
                source=SourceInfo(type="manual"),
                project=task.project,
                workspace=WorkspaceInfo(),
                skills_required=task.skills_required,
                priority=task.priority,
                depends_on=[],
                assigned_to=assigned_to,
                related_task=task.id,
                claimed_by=None,
                claimed_at=None,
                generation=0,
                status="backlog",
                requirement_md=requirement_md,
                acceptance_md="- [ ] 審查報告已寫回本任務執行報告(不開 PR、不留 PR comment)",
                report_md="",
            )
            hubfs.write_task(paths.backlog / f"{review_id}.md", review_task)
        _emit_dashboard_event(
            paths, "review_task_created", review_id, assigned_to, {"related_task": task.id}
        )
        return review_task.model_dump(mode="json")

    return app


_MODEL_FLAGS = ("--model", "-m", "--models")


def _model_on_command(command: list[str]) -> str | None:
    for flag, value in zip(command, command[1:]):
        if flag in _MODEL_FLAGS:
            return value
    return None


def _runtime_of(config, agent: str | None) -> str | None:
    if agent is None:
        return None
    agent_config = config.agents.get(agent)
    return agent_config.runtime if agent_config is not None else None


def _is_independent_reviewer(
    config, reviewer: str, author: str | None, author_runtime: str | None
) -> bool:
    if reviewer == author:
        return False
    if author_runtime is None:
        return True
    return _runtime_of(config, reviewer) != author_runtime


def _task_summary(task: TaskFile) -> dict:
    return {
        "id": task.id,
        "type": task.type,
        "title": task.title,
        "project": task.project,
        "priority": task.priority,
        "claimed_by": task.claimed_by,
        "generation": task.generation,
        "status": task.status,
        "related_task": task.related_task,
    }


def _iter_board_files(paths: HubPaths) -> Iterator[tuple[str, Path]]:
    for path in hubfs.list_task_files(paths.backlog):
        yield "backlog", path
    for agent_dir in hubfs.list_in_progress_agent_dirs(paths.in_progress_root):
        for path in hubfs.list_task_files(agent_dir):
            yield "in_progress", path
    for path in hubfs.list_task_files(paths.blocked):
        yield "blocked", path
    for path in hubfs.list_task_files(paths.review):
        yield "review", path
    for path in hubfs.list_task_files(paths.done):
        yield "done", path


def _find_task_path(paths: HubPaths, task_id: str) -> Path | None:
    candidates = [paths.backlog / f"{task_id}.md"]
    for agent_dir in hubfs.list_in_progress_agent_dirs(paths.in_progress_root):
        candidates.append(agent_dir / f"{task_id}.md")
    candidates += [
        paths.blocked / f"{task_id}.md",
        paths.review / f"{task_id}.md",
        paths.done / f"{task_id}.md",
        paths.invalid / f"{task_id}.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_task_or_500(path: Path) -> TaskFile:
    try:
        return hubfs.read_task(path)
    except TaskFileParseError as exc:
        raise HTTPException(status_code=500, detail=f"task file failed to parse: {exc}") from exc


def _require_task_in(
    paths: HubPaths, task_id: str, allowed: list[tuple[str, Path]]
) -> tuple[Path, TaskFile, str]:
    for name, dir_path in allowed:
        candidate = dir_path / f"{task_id}.md"
        if candidate.is_file():
            return candidate, _read_task_or_500(candidate), name
    if _find_task_path(paths, task_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"task {task_id} is not in an actionable state for this operation"
        )
    raise HTTPException(status_code=404, detail=f"task {task_id} not found")


def _generate_task_id(paths: HubPaths, now: datetime) -> str:
    date_str = now.strftime("%Y%m%d")
    max_seq = 0
    for path in paths.tasks_root.glob("**/*.md"):
        match = _TASK_ID_RE.match(path.stem)
        if match and match.group(1) == date_str:
            max_seq = max(max_seq, int(match.group(2)))
    return f"T-{date_str}-{max_seq + 1:03d}"


def _parse_session_log(raw: str) -> list[dict]:
    entries: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "assistant":
            message = data.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    entries.append({"kind": "say", "text": str(block["text"])[:600]})
                elif block.get("type") == "tool_use":
                    name = str(block.get("name", "?"))
                    entries.append({"kind": "tool", "text": (name + " " + _tool_detail(block.get("input"))).strip()[:300]})
        elif kind == "result" and isinstance(data.get("result"), str):
            entries.append({"kind": "result", "text": data["result"][:600]})
        elif kind == "system" and data.get("subtype") == "init":
            entries.append({"kind": "sys", "text": "session 啟動"})
        elif kind == "thread.started":
            entries.append({"kind": "sys", "text": "Codex session 啟動"})
        elif kind in ("item.started", "item.completed"):
            item = data.get("item")
            entry = _codex_item_entry(kind, item)
            if entry is not None:
                entries.append(entry)
        elif kind == "turn.completed":
            entries.append({"kind": "result", "text": "Codex turn 完成"})
    return entries


def _codex_item_entry(event_type: str, item) -> dict | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if (
        event_type == "item.completed"
        and item_type == "agent_message"
        and isinstance(item.get("text"), str)
    ):
        return {"kind": "say", "text": item["text"][:600]}
    if item_type == "command_execution" and isinstance(item.get("command"), str):
        command = item["command"]
        if event_type == "item.started":
            return {"kind": "tool", "text": command[:300]}
        output = item.get("aggregated_output")
        text = command if not isinstance(output, str) or not output.strip() else f"{command}\n{output.strip()}"
        return {"kind": "result", "text": text[:600]}
    if item_type == "mcp_tool_call":
        server = str(item.get("server", "mcp"))
        tool = str(item.get("tool", "?"))
        text = f"{server}.{tool}"
        suffix = "" if event_type == "item.started" else " 完成"
        return {
            "kind": "tool" if event_type == "item.started" else "result",
            "text": (text + suffix)[:600],
        }
    if item_type == "web_search":
        query = item.get("query")
        if not isinstance(query, str):
            action = item.get("action")
            query = action.get("query") if isinstance(action, dict) else None
        text = "web_search" + (f" {query}" if isinstance(query, str) and query else "")
        suffix = "" if event_type == "item.started" else " 完成"
        return {
            "kind": "tool" if event_type == "item.started" else "result",
            "text": (text + suffix)[:600],
        }
    if (
        event_type == "item.completed"
        and item_type == "error"
        and isinstance(item.get("message"), str)
    ):
        return {"kind": "result", "text": f"Codex 錯誤: {item['message']}"[:600]}
    return None


def _tool_detail(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "description", "file_path", "pattern", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    return ""


def _emit_dashboard_event(
    paths: HubPaths, event: str, task_id: str | None, agent: str | None, detail: dict | None = None
) -> None:
    hubfs.emit_event(
        paths.dashboard_events,
        "dashboard",
        event,
        datetime.now(timezone.utc),
        task_id=task_id,
        agent=agent,
        detail=detail,
    )
