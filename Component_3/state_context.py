"""Point-in-time tenant state adapter for grounded Component 3 reasoning."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from Component_1.event_store import reconstruct_state_at


def reconstruct_reasoning_state(tenant_id: str, at: datetime) -> dict[str, Any]:
    """Return the event-reconstructed tenant state for an auditable run.

    The caller chooses which facts to place in an Opportunity/ReasoningContext;
    this helper never exposes a database handle or execution capability to an
    LLM.
    """
    return reconstruct_state_at(tenant_id, at)
