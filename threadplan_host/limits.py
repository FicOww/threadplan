"""One resource-budget contract shared by autonomous and host-driven sessions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanningLimits:
    max_model_calls: int = 4
    max_read_calls: int = 8
    max_candidates: int = 50
    max_input_bytes: int = 128000
    deadline_seconds: int = 180
    model_timeout_seconds: int = 45
    read_timeout_seconds: int = 20
    max_range_days: int = 31

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("invalid_planning_limits")
        if self.max_model_calls > 8 or self.max_read_calls > 32 or self.max_range_days > 366:
            raise ValueError("oversized_planning_limits")
