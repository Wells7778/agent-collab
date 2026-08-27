from __future__ import annotations

import subprocess
from pathlib import Path

from agenthub.schema import ProjectConfig, TaskFile


class ProvisionError(RuntimeError):
    pass


def ensure_base_clone(workspaces_root: Path, project: str, project_cfg: ProjectConfig) -> Path:
    base_dir = workspaces_root / project / ".base"
    if base_dir.is_dir():
        _run(["git", "fetch", "--prune", "origin"], cwd=base_dir)
    else:
        base_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", project_cfg.repo, str(base_dir)], cwd=base_dir.parent)
    return base_dir


def provision_workspace(
    workspaces_root: Path,
    task_dir: Path,
    task: TaskFile,
    project_cfg: ProjectConfig,
) -> Path:
    if not task.workspace.branch:
        raise ProvisionError(f"task {task.id} has no workspace.branch assigned")

    base_dir = ensure_base_clone(workspaces_root, task.project, project_cfg)

    if (task_dir / ".git").exists():
        _run(["git", "checkout", "-B", task.workspace.branch], cwd=task_dir)
    else:
        task_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                task.workspace.branch,
                str(task_dir),
                f"origin/{project_cfg.default_branch}",
            ],
            cwd=base_dir,
        )

    _run_shell_steps(task_dir, project_cfg.setup_secrets)
    _run_shell_steps(task_dir, project_cfg.setup)
    return task_dir


def sync_knowledge(hub_dir: Path, project: str, project_cfg: ProjectConfig) -> list[Path]:
    synced: list[Path] = []
    for raw in project_cfg.knowledge_paths:
        src = Path(raw).expanduser()
        if not src.is_dir():
            continue
        dest = hub_dir / "knowledge" / project / src.name
        dest.mkdir(parents=True, exist_ok=True)
        _run(["rsync", "-a", "--delete", f"{src}/", f"{dest}/"], cwd=hub_dir)
        synced.append(dest)
    return synced


def _run_shell_steps(cwd: Path, steps: list[str]) -> None:
    for step in steps:
        result = subprocess.run(step, shell=True, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ProvisionError(f"setup step failed: {step}\n{_tail(result.stderr)}")


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProvisionError(f"command failed: {' '.join(cmd)}\n{_tail(result.stderr)}")


def _tail(text: str, lines: int = 20) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])
