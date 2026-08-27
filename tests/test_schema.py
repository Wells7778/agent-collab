from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agenthub.schema import HubReport, TaskFile, TaskFileParseError
from conftest import REPO_ROOT, make_task


def test_roundtrip_preserves_fields():
    task = make_task(
        skills_required=["rust", "python"],
        depends_on=["T-20260825-001"],
        requirement_md="需求內容\n多行",
        acceptance_md="- [ ] a\n- [ ] b",
        report_md="",
    )
    text = task.to_markdown()
    reloaded = TaskFile.from_markdown(text)
    assert reloaded == task


def test_roundtrip_preserves_report_after_reload():
    task = make_task(report_md="### Checkpoint\n\nsome progress")
    reloaded = TaskFile.from_markdown(task.to_markdown())
    assert reloaded.report_md == "### Checkpoint\n\nsome progress"


def test_real_template_parses():
    text = (REPO_ROOT / "templates" / "T-000-template.md").read_text()
    task = TaskFile.from_markdown(text)
    assert task.id == "T-000"
    assert task.project == "my-service"
    assert task.priority == "P2"


def test_missing_frontmatter_delimiter_raises():
    with pytest.raises(TaskFileParseError):
        TaskFile.from_markdown("no frontmatter here\n## 需求描述\n## 驗收標準\n## 執行報告\n")


def test_missing_body_section_raises():
    broken = "---\nid: T-1\ntype: coding\ntitle: x\nsource: {type: manual}\nproject: proj-a\npriority: P2\n---\n\n## 需求描述\n\nfoo\n"
    with pytest.raises(TaskFileParseError):
        TaskFile.from_markdown(broken)


def test_section_headers_inside_the_report_do_not_split_the_body():
    task = make_task(
        requirement_md="原始需求",
        acceptance_md="- [ ] 驗收項",
        report_md="## 盤點結果\n\n## 需求描述\n\n這是報告內容,不是需求",
    )

    back = TaskFile.from_markdown(task.to_markdown())

    assert back.requirement_md == "原始需求"
    assert back.acceptance_md == "- [ ] 驗收項"
    assert back.report_md == task.report_md


def test_a_report_naming_every_section_still_round_trips():
    report_md = "## 執行報告\n## 驗收標準\n## 需求描述\n尾段"
    task = make_task(requirement_md="需求", acceptance_md="驗收", report_md=report_md)

    back = TaskFile.from_markdown(task.to_markdown())

    assert back.requirement_md == "需求"
    assert back.report_md == report_md


def test_invalid_priority_rejected():
    with pytest.raises(ValidationError):
        make_task(priority="P9")


def test_hub_report_checkpoint_requires_summary_and_report_md():
    HubReport(kind="checkpoint", task_id="T-1", summary="s", report_md="r")
    with pytest.raises(ValidationError):
        HubReport(kind="checkpoint", task_id="T-1", summary="s", report_md=None)


def test_hub_report_blocked_requires_question():
    HubReport(kind="blocked", task_id="T-1", question="why?")
    with pytest.raises(ValidationError):
        HubReport(kind="blocked", task_id="T-1", question=None)


def test_hub_report_final_requires_result_summary_report_md():
    HubReport(kind="final", task_id="T-1", result="completed", summary="s", report_md="r")
    with pytest.raises(ValidationError):
        HubReport(kind="final", task_id="T-1", result=None, summary="s", report_md="r")


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "checkpoint", "task_id": "T-1", "summary": None, "report_md": "r"},
        {"kind": "checkpoint", "task_id": "T-1", "summary": "s", "report_md": None},
        {"kind": "blocked", "task_id": "T-1", "question": None},
        {"kind": "final", "task_id": "T-1", "result": None, "summary": "s", "report_md": "r"},
        {"kind": "final", "task_id": "T-1", "result": "completed", "summary": None, "report_md": "r"},
        {"kind": "final", "task_id": "T-1", "result": "completed", "summary": "s", "report_md": None},
    ],
)
def test_hub_report_rejects_each_missing_required_field(payload):
    with pytest.raises(ValidationError):
        HubReport(**payload)


def test_enabled_role_without_a_runtime_is_rejected():
    from pydantic import ValidationError

    from agenthub.schema import AgentConfig

    with pytest.raises(ValidationError, match="must declare `runtime`"):
        AgentConfig.model_validate({"enabled": True, "skills": ["general"]})


def test_disabled_role_may_omit_its_runtime():
    from agenthub.schema import AgentConfig

    assert AgentConfig.model_validate({"enabled": False}).runtime is None
