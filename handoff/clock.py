"""One clock for the whole app, so the simulator and screenshots can control time.

Modes:
- real time (default)
- fixed: HANDOFF_NOW=2026-10-08T06:50 freezes the clock (screenshots, tests)
- accelerated: the demo starts the morning at 05:55 and runs it N times faster
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta


class Clock:
    def __init__(self) -> None:
        self.fixed: datetime | None = None
        self.start: datetime | None = None
        self.speed: float = 1.0
        self._t0 = time.monotonic()
        env = os.getenv("HANDOFF_NOW")
        if env:
            self.fixed = datetime.fromisoformat(env)

    def set_fixed(self, when: datetime) -> None:
        self.fixed, self.start = when, None

    def accelerate(self, start: datetime, speed: float) -> None:
        self.fixed, self.start, self.speed, self._t0 = None, start, speed, time.monotonic()

    def real(self) -> None:
        self.fixed = self.start = None

    @property
    def frozen(self) -> bool:
        return self.fixed is not None

    def now(self) -> datetime:
        if self.fixed is not None:
            return self.fixed
        if self.start is not None:
            return self.start + timedelta(seconds=(time.monotonic() - self._t0) * self.speed)
        return datetime.now().replace(microsecond=0)

    def sleep_seconds(self, sim_seconds: float) -> float:
        """Real seconds to sleep for a span of simulated seconds."""
        return sim_seconds / self.speed if self.start is not None else sim_seconds


CLOCK = Clock()


def now() -> datetime:
    return CLOCK.now().replace(microsecond=0)
