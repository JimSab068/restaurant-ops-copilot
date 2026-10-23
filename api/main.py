"""
API service entrypoint.

Every route that touches tenant data depends on require_tenant (auth.py),
which is the ONLY source of tenant_id in this whole service. The request
body's contents are never used to determine which tenant a write belongs
to -- see api/auth.py's docstring for why that matters.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import IntegrityError

from Component_1.db import get_db_context, health_check
from Component_1.models import ActionType, MenuItem

from .auth import require_tenant
from .schemas import (
    ActionAcceptedResponse,
    ActionRequest,
    ActionStatusResponse,
    CycleRequest,
    CycleResponse,
    MenuItemPriceResponse,
)
from .work_request_models import WorkRequest
from shared.logging import configure_logging, log_context
from shared.queue import publish_work_request

from Component_3.llm_client import LLMClientError
from Component_3.llm_client_bedrock import BedrockJSONLLMClient
from Component_3.llm_reasoner import LLMReasoner
from orchestrator import CycleMode, PipelineConfigError, run_cycle

configure_logging()
logger = logging.getLogger("api")

app = FastAPI(title="Restaurant Ops Copilot API")

_VALID_ACTION_TYPES = {member.value for member in ActionType}


@app.post(
    "/v1/actions",
    response_model=ActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_action(
    request: ActionRequest,
    tenant_id: str = Depends(require_tenant),
) -> ActionAcceptedResponse:
    if request.action_type not in _VALID_ACTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown action_type: {request.action_type!r}",
        )

    with get_db_context(tenant_id) as session:
        existing = (
            session.query(WorkRequest)
            .filter(
                WorkRequest.tenant_id == tenant_id,
                WorkRequest.idempotency_key == request.idempotency_key,
            )
            .one_or_none()
        )

        if existing is not None:
            # Client-facing idempotency: re-submitting the same
            # idempotency_key returns the existing request rather than
            # creating a duplicate or erroring.
            logger.info(
                "Idempotent resubmission, returning existing request",
                extra=log_context(
                    tenant_id=tenant_id, work_request_id=existing.id,
                ),
            )
            return ActionAcceptedResponse(
                request_id=existing.id,
                status=existing.status,
                idempotency_key=existing.idempotency_key,
            )

        work_request = WorkRequest(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            idempotency_key=request.idempotency_key,
            action_type=request.action_type,
            payload=request.payload,
            status="pending",
        )

        try:
            session.add(work_request)
            session.flush()
        except IntegrityError:
            # Race: two concurrent submits with the same idempotency_key.
            # The unique constraint is the real guard; fetch and return
            # whichever one won.
            session.rollback()
            existing = (
                session.query(WorkRequest)
                .filter(
                    WorkRequest.tenant_id == tenant_id,
                    WorkRequest.idempotency_key == request.idempotency_key,
                )
                .one()
            )
            return ActionAcceptedResponse(
                request_id=existing.id,
                status=existing.status,
                idempotency_key=existing.idempotency_key,
            )

        work_request_id = work_request.id

    # Published AFTER the DB commit (get_db_context commits on successful
    # exit), so a worker can never receive a message for a row that isn't
    # durably there yet.
    publish_work_request(tenant_id, work_request_id)

    logger.info(
        "Work request accepted",
        extra=log_context(tenant_id=tenant_id, work_request_id=work_request_id),
    )

    return ActionAcceptedResponse(
        request_id=work_request_id,
        status="pending",
        idempotency_key=request.idempotency_key,
    )


@app.get("/v1/actions/{request_id}", response_model=ActionStatusResponse)
def get_action_status(
    request_id: str,
    tenant_id: str = Depends(require_tenant),
) -> ActionStatusResponse:
    with get_db_context(tenant_id) as session:
        work_request = (
            session.query(WorkRequest)
            .filter(
                WorkRequest.tenant_id == tenant_id,
                WorkRequest.id == request_id,
            )
            .one_or_none()
        )

        if work_request is None:
            # RLS means a request for another tenant's ID lands here too
            # -- indistinguishable from "doesn't exist", which is correct:
            # a 404 must not confirm or deny another tenant's data exists.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="request not found",
            )

        return ActionStatusResponse(
            request_id=work_request.id,
            status=work_request.status,
            action_type=work_request.action_type,
            payload=work_request.payload,
            result=work_request.result,
            error=work_request.error,
            created_at=work_request.created_at,
            updated_at=work_request.updated_at,
            completed_at=work_request.completed_at,
        )


@app.get(
    "/v1/menu-items/{menu_item_id}",
    response_model=MenuItemPriceResponse,
)
def get_menu_item_price(
    menu_item_id: str,
    tenant_id: str = Depends(require_tenant),
) -> MenuItemPriceResponse:
    """Return the current price of a menu item owned by the authenticated tenant."""
    with get_db_context(tenant_id) as session:
        menu_item = (
            session.query(MenuItem)
            .filter(
                MenuItem.id == menu_item_id,
                MenuItem.tenant_id == tenant_id,
                MenuItem.active.is_(True),
            )
            .one_or_none()
        )

        if menu_item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="menu item not found",
            )

        return MenuItemPriceResponse(
            menu_item_id=menu_item.id,
            current_price=float(menu_item.current_price),
        )


@app.post("/v1/cycles", response_model=CycleResponse)
def run_operational_cycle(
    request: CycleRequest,
    tenant_id: str = Depends(require_tenant),
) -> CycleResponse:
    """Run the real Component 1 -> 7 cycle for the authenticated tenant.

    This endpoint is deliberately separate from ``/v1/actions``: actions
    submitted there begin at Component 4, whereas this route begins with
    Component 1 state and includes Component 3 opportunity detection and
    optional LLM reasoning. Tenant identity comes only from the API key.
    """
    llm_reasoner = None
    if request.use_llm:
        try:
            llm_reasoner = LLMReasoner(BedrockJSONLLMClient())
        except LLMClientError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LLM reasoning is unavailable",
            ) from exc

    try:
        report = run_cycle(
            tenant_id,
            mode=CycleMode(request.mode),
            llm_reasoner=llm_reasoner,
            use_llm=request.use_llm,
            max_opportunities=request.max_opportunities,
            max_actions=request.max_actions,
            # A cycle exposed by the deployed API must verify the database
            # isolation boundary rather than relying only on application
            # filters. Local pipeline tests call run_cycle() directly.
            require_rls=True,
        )
    except PipelineConfigError as exc:
        logger.exception("Cycle preflight rejected", extra=log_context(tenant_id=tenant_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cycle execution is unavailable until RLS is enforced",
        ) from exc

    return CycleResponse(**{
        key: report.to_dict()[key]
        for key in CycleResponse.model_fields
    })


@app.get("/healthz")
def health() -> dict[str, str]:
    """Liveness only: is the process up and able to answer HTTP at all.

    Deliberately does not touch the database -- a DB outage should not
    make ECS think the API process itself needs restarting. See /readyz
    for the DB-backed check that should gate ALB target-group traffic.
    """
    return {"status": "ok"}


@app.get("/readyz")
def ready() -> dict[str, str]:
    """Readiness: can this task actually serve traffic right now.

    Backed by Component_1.db.health_check() (a plain `SELECT 1`). Point
    the ALB target group's health check and the ECS task definition's
    readiness probe at this route, not /healthz -- a task that's up but
    can't reach Postgres should be pulled out of rotation, not treated
    as healthy.
    """
    if not health_check():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database is not reachable",
        )

    return {"status": "ready"}