"""Pydantic schemas for the API service's request/response bodies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Kept as plain strings (not the Component_1 ActionType enum) at the
# schema boundary so a malformed action_type produces a clean 422 rather
# than an import-time coupling issue; worker/handler.py does the real
# ActionType(...) validation against Component 1's enum.
_ALLOWED_ACTION_TYPES = (
    "price_change",
    "supplier_switch",
    "menu_swap",
    "staffing_change",
)


class ActionRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    action_type: str = Field(..., description="One of: " + ", ".join(_ALLOWED_ACTION_TYPES))
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionAcceptedResponse(BaseModel):
    request_id: str
    status: str
    idempotency_key: str


class ActionStatusResponse(BaseModel):
    request_id: str
    status: str
    action_type: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class MenuItemPriceResponse(BaseModel):
    menu_item_id: str
    current_price: float


class CycleRequest(BaseModel):
    """Tenant-scoped request to run the complete operational loop.

    ``simulate`` is the safe default. A caller must explicitly request
    ``execute`` before the cycle can reach the action framework.
    """

    mode: Literal["propose", "simulate", "execute"] = "simulate"
    use_llm: bool = True
    max_opportunities: int = Field(default=10, ge=1, le=100)
    max_actions: int = Field(default=3, ge=0, le=10)


class CycleResponse(BaseModel):
    cycle_id: str
    tenant_id: str
    mode: str
    status: str
    reason: str | None
    actions_attempted: int
    llm: dict[str, Any] | None
    outcomes: list[dict[str, Any]]
