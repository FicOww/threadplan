"""Public embedding API; host tools do not import the autonomous coordinator."""

from importlib import import_module

_EXPORTS = {"ConversationPlanner": "planning", "PlanningLimits": "limits",
            "PlanningStore": "planning_store", "PlanningExecutor": "planning_execution",
            "AgentSession": "agent"}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("." + _EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
