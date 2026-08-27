from __future__ import annotations

import time
from datetime import datetime, timezone

from agenthub.scheduler import Scheduler


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def run_forever(scheduler: Scheduler) -> None:
    scheduler.startup_scan()
    while True:
        scheduler.tick()
        time.sleep(scheduler.config.heartbeat_seconds)
