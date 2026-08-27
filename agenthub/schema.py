from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

TaskType = Literal["coding", "review", "research", "explore"]
Priority = Literal["P0", "P1", "P2", "P3"]
TaskStatus = Literal["backlog", "in-progress", "blocked", "review", "done", "cancelled"]
AgentState = Literal["working", "idle", "resting", "offline"]
AgentPhase = Literal["setup", "running", "reporting"]
ReportKind = Literal["checkpoint", "blocked", "final"]
ReportResult = Literal["completed", "failed"]
EventActor = Literal["daemon", "dashboard"]
EventType = Literal[
    "daemon_started",
    "probe_failed",
    "task_invalid",
    "task_dispatched",
    "agent_spawned",
    "spawn_failed",
    "task_checkpoint",
    "report_parse_failed",
    "task_no_report",
    "task_blocked",
    "task_review_ready",
    "task_done",
    "agent_exited",
    "agent_timeout",
    "agent_rate_limited",
    "task_requeued",
    "task_created",
    "task_replied",
    "task_returned",
    "task_completed",
    "task_cancelled",
    "review_task_created",
    "review_pair_unavailable",
]

DEFAULT_AGENT_PROBE = ["claude", "--version"]
DEFAULT_AGENT_COMMAND = [
    "caffeinate",
    "-i",
    "claude",
    "-p",
    "--output-format",
    "json",
    "--dangerously-skip-permissions",
]

_BODY_SECTIONS = ("需求描述", "驗收標準", "執行報告")
_SECTION_HEADER_RE = re.compile(
    r"^## (" + "|".join(re.escape(name) for name in _BODY_SECTIONS) + r")[ \t]*\n",
    re.MULTILINE,
)


class TaskFileParseError(ValueError):
    pass


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["manual", "asana"]
    asana_url: str | None = None


class WorkspaceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str | None = None
    branch_base: str | None = None
    branch: str | None = None


class TaskFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: TaskType
    title: str
    source: SourceInfo
    project: str
    workspace: WorkspaceInfo = Field(default_factory=WorkspaceInfo)
    skills_required: list[str] = Field(default_factory=list)
    priority: Priority
    depends_on: list[str] = Field(default_factory=list)
    assigned_to: str | None = None
    related_task: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    generation: int = 0
    status: TaskStatus = "backlog"

    requirement_md: str = ""
    acceptance_md: str = ""
    report_md: str = ""

    @classmethod
    def from_markdown(cls, text: str) -> "TaskFile":
        frontmatter, body = _split_frontmatter(text)
        try:
            data = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            raise TaskFileParseError(f"invalid frontmatter yaml: {exc}") from exc
        if not isinstance(data, dict):
            raise TaskFileParseError("frontmatter yaml is not a mapping")
        sections = _parse_body_sections(body)
        data["requirement_md"] = sections["需求描述"]
        data["acceptance_md"] = sections["驗收標準"]
        data["report_md"] = sections["執行報告"]
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise TaskFileParseError(f"task file failed schema validation: {exc}") from exc

    def to_markdown(self) -> str:
        raw = self.model_dump(mode="json", exclude={"requirement_md", "acceptance_md", "report_md"})
        frontmatter = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False)
        body = (
            f"## 需求描述\n\n{self.requirement_md.strip()}\n\n"
            f"## 驗收標準\n\n{self.acceptance_md.strip()}\n\n"
            f"## 執行報告\n\n{self.report_md.strip()}\n"
        )
        return f"---\n{frontmatter}---\n\n{body}"

    def with_report_appended(self, entry: str) -> "TaskFile":
        return self.model_copy(update={"report_md": (self.report_md + "\n\n" + entry).strip()})


class HubReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReportKind
    task_id: str
    summary: str | None = None
    report_md: str | None = None
    result: ReportResult | None = None
    pr_url: str | None = None
    question: str | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> "HubReport":
        if self.kind == "checkpoint":
            if not self.summary or not self.report_md:
                raise ValueError("checkpoint report requires summary and report_md")
        elif self.kind == "blocked":
            if not self.question:
                raise ValueError("blocked report requires question")
        elif self.kind == "final":
            if not self.result or not self.summary or not self.report_md:
                raise ValueError("final report requires result, summary and report_md")
        return self


class AgentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    state: AgentState
    task_id: str | None = None
    project: str | None = None
    phase: AgentPhase | None = None
    pid: int | None = None
    pgid: int | None = None
    started_at: datetime | None = None
    cooldown_until: datetime | None = None
    heartbeat_at: datetime


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    actor: EventActor
    event: EventType
    task_id: str | None = None
    agent: str | None = None
    detail: dict = Field(default_factory=dict)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    runtime: str | None = None
    prompt: str | None = None
    skills: list[str] = Field(default_factory=list)
    task_types: list[TaskType] = Field(default_factory=lambda: ["coding", "review"])
    command: list[str] = Field(default_factory=lambda: DEFAULT_AGENT_COMMAND)
    probe: list[str] = Field(default_factory=lambda: DEFAULT_AGENT_PROBE)

    @model_validator(mode="after")
    def _enabled_role_declares_its_runtime(self) -> "AgentConfig":
        if self.enabled and self.runtime is None:
            raise ValueError(
                "an enabled role must declare `runtime`; "
                "pair review relies on it to keep reviewers off the author's runtime"
            )
        return self


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces_root: str
    spawn_env: dict[str, str] = Field(default_factory=dict)
    max_concurrent_global: int
    max_concurrent_per_branch_base: int
    max_concurrent_per_agent: int
    task_timeout_minutes: int
    heartbeat_seconds: int
    worktree_retention_days: int
    max_generation: int
    rate_limit_cooldown_minutes: int = 30
    agents: dict[str, AgentConfig]


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    default_branch: str
    setup: list[str] = Field(default_factory=list)
    setup_secrets: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)
    spec_paths: list[str] = Field(default_factory=list)
    knowledge_paths: list[str] = Field(default_factory=list)
    allowed_agents: list[str] = Field(default_factory=list)


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise TaskFileParseError("missing frontmatter opening delimiter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise TaskFileParseError("missing frontmatter closing delimiter")
    frontmatter = text[4:end]
    body = text[end + 5 :]
    return frontmatter, body


def _parse_body_sections(body: str) -> dict[str, str]:
    bounds: list[tuple[str, int, int]] = []
    cursor = 0
    missing: list[str] = []
    for name in _BODY_SECTIONS:
        match = _SECTION_HEADER_RE.search(body, cursor)
        while match is not None and match.group(1) != name:
            match = _SECTION_HEADER_RE.search(body, match.end())
        if match is None:
            missing.append(name)
            continue
        bounds.append((name, match.start(), match.end()))
        cursor = match.end()
    if missing:
        raise TaskFileParseError(f"missing body sections: {missing}")
    sections: dict[str, str] = {}
    for idx, (name, _, start) in enumerate(bounds):
        end = bounds[idx + 1][1] if idx + 1 < len(bounds) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def parse_frontmatter_lenient(text: str) -> dict | None:
    try:
        frontmatter, _ = _split_frontmatter(text)
    except TaskFileParseError:
        return None
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None
