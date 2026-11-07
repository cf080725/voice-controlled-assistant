"""Trusted server resource policy, independent of any model throughput claim."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceAdmissionLimits:
    client_pending: int = 8
    global_pending: int = 32
    prepare_concurrency: int = 2

    def __post_init__(self):
        for name in ("client_pending", "global_pending", "prepare_concurrency"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + " must be a positive integer")
