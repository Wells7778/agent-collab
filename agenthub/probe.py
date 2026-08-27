from __future__ import annotations

import subprocess

from agenthub import hubfs
from agenthub.hubfs import HubPaths
from agenthub.schema import AgentStatus, HubConfig
from agenthub.scheduler import Clock


def run_probes(config: HubConfig) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for agent, agent_cfg in config.agents.items():
        if not agent_cfg.enabled:
            continue
        results[agent] = _probe_ok(agent_cfg.probe)
    return results


def apply_probe_results(
    config: HubConfig, results: dict[str, bool], paths: HubPaths, clock: Clock
) -> HubConfig:
    agents = dict(config.agents)
    for agent, ok in results.items():
        if ok:
            continue
        agents[agent] = agents[agent].model_copy(update={"enabled": False})
        hubfs.write_status(
            paths.status(agent),
            AgentStatus(agent=agent, state="offline", heartbeat_at=clock.now()),
        )
        hubfs.emit_event(
            paths.events,
            "daemon",
            "probe_failed",
            clock.now(),
            agent=agent,
            detail={"probe": agents[agent].probe},
        )
    return config.model_copy(update={"agents": agents})


def _probe_ok(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command, capture_output=True, stdin=subprocess.DEVNULL, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
