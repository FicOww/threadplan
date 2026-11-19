"""Provider-independent, JSON-serializable domain contracts."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Operation = Literal["CREATE", "UPDATE", "CANCEL"]
Commitment = Literal["tentative", "confirmed", "cancelled"]


@dataclass(frozen=True)
class Event:
    id: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Proposal:
    operation: Operation
    commitment_state: Commitment
    evidence_message_ids: tuple[str, ...]
    changes: dict[str, Any] = field(default_factory=dict)
    target_event_id: str | None = None
    unresolved_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_message_ids"] = list(self.evidence_message_ids)
        data["unresolved_fields"] = list(self.unresolved_fields)
        return data
