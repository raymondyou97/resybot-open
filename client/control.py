"""Cooperative worker cancellation and monotonic execution deadlines."""

from dataclasses import dataclass, field
import math
import threading
import time


@dataclass
class RunControl:
    duration: float | str = 120
    event: threading.Event = field(default_factory=threading.Event)
    clock: object = time.monotonic

    def __post_init__(self):
        if self.duration == 'forever':
            self.deadline = math.inf
            return
        if (
            type(self.duration) not in (int, float)
            or not math.isfinite(self.duration)
            or not 0 < self.duration <= 86400
        ):
            raise ValueError('Duration must be greater than 0 and at most 86400 seconds, or forever.')
        self.deadline = self.clock() + self.duration

    def stopped(self):
        return self.event.is_set() or self.clock() >= self.deadline

    def stop(self):
        self.event.set()

    def wait(self, seconds):
        remaining = max(0, self.deadline - self.clock())
        self.event.wait(min(max(0, seconds), remaining))
        return self.stopped()

    def timeout(self):
        remaining = max(0.1, self.deadline - self.clock())
        return (min(3, remaining), min(5, remaining))


def parse_duration(value):
    duration = 'forever' if value == 'forever' else float(value)
    RunControl(duration)
    return duration
