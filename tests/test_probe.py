from __future__ import annotations

from pathlib import Path

from agenthub import hubfs, probe
from agenthub.hubfs import HubPaths
from conftest import FakeClock


def test_apply_probe_results_disables_failed_agent_and_records_status_and_event(hub_dir: Path):
    paths = HubPaths(hub_dir)
    config = hubfs.load_config(paths.config_file)
    clock = FakeClock()

    effective = probe.apply_probe_results(config, {"claude": True, "codex": False}, paths, clock)

    assert effective.agents["codex"].enabled is False
    assert effective.agents["claude"].enabled is True

    status = hubfs.read_status(paths.status("codex"))
    assert status is not None
    assert status.state == "offline"

    events = hubfs.read_events(paths.events)
    assert any(e.event == "probe_failed" and e.agent == "codex" for e in events)


def test_apply_probe_results_all_pass_leaves_config_unchanged(hub_dir: Path):
    paths = HubPaths(hub_dir)
    config = hubfs.load_config(paths.config_file)
    clock = FakeClock()

    effective = probe.apply_probe_results(config, {"claude": True, "codex": True}, paths, clock)

    assert effective == config
    assert hubfs.read_events(paths.events) == []
