"""
Top-level operating loop for Restaurant Ops Copilot (Components 1-7).

    Preflight ─► Detect ─► Reason ─► Simulate & rank ─► Gate ─► Act ─► Observe
       │           │          │            │              │       │        │
   C5 RLS +     C3 detector  C3 det. +   C2 simulator   C4 critic  C4 tool  C2 ground
   C1 chain     (read-only)  optional    (read-only,    C6 risk    frame-   truth +
   integrity                 LLM         scored)        approval   work     calibration
                                                                            │
                                            every stage ─► C7 observer hook ┘

Design rules (from the README's engineering principles)
-------------------------------------------------------
* The LLM is never the system of record: reasoning only proposes.
* Reasoning does not imply authority: only tool_framework.execute_tool_call
  executes, and it applies simulator confidence, critic, and risk policy.
* Uncertainty reduces authority: the default mode never acts, a corrupted
  event chain skips the cycle, and any doubt about review routes to a human.
* Blast radius is bounded: per-cycle action cap, per-target cooldown, one
  cycle at a time per tenant (Postgres advisory lock).
* Every outcome is reported; failures are contained per opportunity.

Entry points
------------
propose_for_tenant(...)   detect + reason only (unchanged from before)
run_cycle(...)            the full loop; returns a CycleReport
resolve_approval(...)     human review step: approve/reject, then execute
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from sqlalchemy import text

from Component_1.db import engine, get_db_context, validate_tenant_id
from Component_1.event_store import (
    EventStoreError,
    HashChainCorruptedError,
    verify_chain_integrity,
)
from Component_1.models import Decision
from Component_2.calibration import overall_calibration
from Component_2.simulator import simulate
from Component_3.models import CandidateAction, CandidateSource, Opportunity
from Component_3.opportunity_detector import detect_opportunities
from Component_3.orchestrator import reason_about_opportunities
from Component_4.critic import critique
from Component_4.tool_framework import (
    _current_value_for_critic,  # read-only helper, reused for re-validation
    approval_workflow,
    execute_approved_tool_call,
    execute_tool_call,
    execute_tool_call_idempotent,
    get_action_log,
)
from Component_6.approval_workflow import ApprovalStatus
from Component_5.enable_rls import ALL_RLS_TABLES

logger = logging.getLogger(__name__)

# Observer hook: Component 7 (or anything else) receives (event_name, payload).
# It must never be able to break the loop, so exceptions are swallowed.
Observer = Callable[[str, Mapping[str, Any]], None]
Scorer = Callable[[Any], float]


class PipelineConfigError(RuntimeError):
    """Raised when the deployment is not safe to run autonomously."""


class CycleMode(str, Enum):
    PROPOSE = "propose"    # detect + reason
    SIMULATE = "simulate"  # + simulate, rank, select (no writes)
    EXECUTE = "execute"    # + act through the tool framework


class OutcomeStatus(str, Enum):
    # Values of the tool framework's own reasons are reused verbatim.
    EXECUTED = "executed"
    PENDING_HUMAN_APPROVAL = "pending_human_approval"
    HELD_LOW_CONFIDENCE = "held_low_confidence"
    BLOCKED_BY_CRITIC = "blocked_by_critic"
    UNSUPPORTED_ACTION = "unsupported_action"
    # Pipeline-level outcomes.
    PROPOSED = "proposed"
    SIMULATED_ONLY = "simulated_only"
    NO_CANDIDATES = "no_candidates"
    NO_BENEFICIAL_ACTION = "no_beneficial_action"
    COOLDOWN_ACTIVE = "cooldown_active"
    ACTION_LIMIT_REACHED = "action_limit_reached"
    NEEDS_REVIEW_NOT_EXECUTED = "needs_review_not_executed"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredCandidate:
    candidate: CandidateAction
    predicted: Any | None
    score: float | None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        c = self.candidate
        data: dict[str, Any] = {
            "action_type": c.action_type.value,
            "payload": dict(c.payload),
            "source": c.source.value,
            "requires_human_review": c.requires_human_review,
            "rationale": c.rationale,
            "score": self.score,
        }
        if self.predicted is not None:
            to_dict = getattr(self.predicted, "to_dict", None)
            data["predicted"] = to_dict() if callable(to_dict) else repr(self.predicted)
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class OpportunityOutcome:
    opportunity_id: str
    opportunity_type: str
    estimated_monthly_impact: float
    status: OutcomeStatus
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected: dict[str, Any] | None = None
    result: dict[str, Any] | None = None  # raw tool-framework result, if attempted
    detail: str | None = None


@dataclass
class CycleReport:
    cycle_id: str
    tenant_id: str
    mode: CycleMode
    started_at: datetime
    status: str = "completed"  # completed | skipped | failed | aborted
    reason: str | None = None
    finished_at: datetime | None = None
    outcomes: list[OpportunityOutcome] = field(default_factory=list)
    calibration: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None  # LLM reasoning health this cycle (None if unused)

    @property
    def actions_attempted(self) -> int:
        return sum(1 for o in self.outcomes if o.result is not None)

    def outcome_for(self, opportunity_id: str) -> OpportunityOutcome | None:
        return next((o for o in self.outcomes if o.opportunity_id == opportunity_id), None)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._raw(), default=str))

    def _raw(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "tenant_id": self.tenant_id,
            "mode": self.mode.value,
            "status": self.status,
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "actions_attempted": self.actions_attempted,
            "calibration": self.calibration,
            "llm": self.llm,
            "outcomes": [
                {**o.__dict__, "status": o.status.value} for o in self.outcomes
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(observer: Observer | None, event: str, tenant_id: str,
          cycle_id: str | None, **data: Any) -> None:
    payload = {"tenant_id": tenant_id, "cycle_id": cycle_id, **data}
    logger.info("pipeline.%s %s", event, {k: payload[k] for k in ("tenant_id", "cycle_id")})
    if observer is None:
        return
    try:
        observer(event, payload)
    except Exception:  # an observer must never break the loop
        logger.exception("Observer failed on event %s", event)


def _cause_chain(exc: BaseException) -> str:
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__
    return " <- ".join(parts)


class _LLMProbe:
    """
    Transparent proxy around an LLM reasoner that records what happened.

    The reasoning layer degrades to deterministic candidates on ANY LLM
    failure, which is the right safety behaviour but makes a dead LLM path
    invisible: a cycle that silently used deterministic reasoning looks
    identical to one that used the model. (Real example: a model that wraps
    its JSON in markdown fences fails every call.) The probe re-raises
    everything unchanged, so fallback behaviour is untouched.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0
        self.failures = 0
        self.candidates = 0
        self.errors: list[str] = []

    def reason(self, opportunity, *, allowed_action_types):
        self.calls += 1
        try:
            result = self._inner.reason(opportunity, allowed_action_types=allowed_action_types)
        except Exception as exc:
            self.failures += 1
            message = _cause_chain(exc)
            if message not in self.errors:
                self.errors.append(message)
            raise
        self.candidates += len(result)
        return result

    def summary(self) -> dict[str, Any]:
        return {"calls": self.calls, "failures": self.failures,
                "candidates": self.candidates, "errors": self.errors[:3]}


def default_scorer(predicted: Any) -> float:
    """
    Confidence-weighted predicted margin change.

    Replace with a dollar-impact scorer when the simulator exposes one. Any
    non-finite or non-positive score means "do not act".
    """
    return float(predicted.confidence) * float(predicted.margin_change_pct)


def assert_rls_enforced() -> None:
    """
    Refuse to run autonomously unless tenant isolation is actually in force:
    every tenant-scoped table has RLS enabled AND the connected role cannot
    bypass it (superusers and BYPASSRLS roles ignore policies).
    """
    tables = sorted(ALL_RLS_TABLES)
    with engine.connect() as conn:
        enabled = set(conn.execute(
            text("SELECT relname FROM pg_class "
                 "WHERE relname = ANY(:names) AND relrowsecurity"),
            {"names": tables},
        ).scalars().all())
        bypass = conn.execute(text(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )).scalar()
    missing = sorted(set(tables) - enabled)
    if missing:
        raise PipelineConfigError(f"RLS is not enabled on: {', '.join(missing)}")
    if bypass:
        raise PipelineConfigError("connected database role bypasses RLS")


@contextmanager
def _tenant_cycle_lock(tenant_id: str):
    """One cycle at a time per tenant (session-level Postgres advisory lock)."""
    key = f"ops-cycle:{tenant_id}"
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
        ).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
                except Exception:
                    logger.exception("Advisory unlock failed; discarding connection")
                    conn.invalidate()
    finally:
        conn.close()


_TARGET_KEYS = ("menu_item_id", "ingredient_id", "supplier_sku_id", "staff_shift_id")


def _target(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple((k, str(payload[k])) for k in _TARGET_KEYS if payload.get(k) is not None)


def _in_cooldown(tenant_id: str, candidate: CandidateAction,
                 cooldown: timedelta, now: datetime) -> bool:
    """
    True if the same action type on the same target was executed or sent to a
    reviewer within the cooldown window. Rejected/held requests count too, so
    a rejected recommendation is not re-asked every cycle. Blocked and
    low-confidence attempts do not (state may since have changed).
    """
    if cooldown <= timedelta(0):
        return False
    since = now - cooldown
    target = _target(candidate.payload)
    for entry in get_action_log(tenant_id, limit=500):  # newest first
        created = entry["created_at"]
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < since:
            break
        if entry["action_type"] != candidate.action_type.value:
            continue
        if _target(entry["payload"] or {}) != target:
            continue
        pending = entry["critic_approved"] is True and not entry["executed"]
        if entry["executed"] or pending:
            return True
    return False


def _idempotency_key(cycle_id: str, opportunity_id: str, candidate: CandidateAction) -> str:
    digest = hashlib.sha256(json.dumps(
        {"a": candidate.action_type.value, "p": dict(candidate.payload)},
        sort_keys=True, default=str,
    ).encode()).hexdigest()[:16]
    return f"{cycle_id}:{opportunity_id}:{digest}"


def _forced_review_supported() -> bool:
    """
    True once tool_framework accepts `require_approval_reason`. Until then a
    candidate flagged requires_human_review is NOT executed (fail closed).
    """
    return all(
        "require_approval_reason" in inspect.signature(fn).parameters
        for fn in (execute_tool_call, execute_tool_call_idempotent)
    )


def _score(tenant_id: str, candidates: list[CandidateAction],
           scorer: Scorer) -> list[ScoredCandidate]:
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        try:
            predicted = simulate(tenant_id, candidate.action_type, dict(candidate.payload))
            score = float(scorer(predicted))
            if not math.isfinite(score):
                raise ValueError("score is not finite")
            scored.append(ScoredCandidate(candidate, predicted, score))
        except Exception as exc:  # one bad candidate must not sink the rest
            scored.append(ScoredCandidate(candidate, None, None,
                                          error=f"{type(exc).__name__}: {exc}"))
    return scored


def _rank_key(s: ScoredCandidate):
    return (
        -(s.score if s.score is not None else -math.inf),
        s.candidate.source is not CandidateSource.DETERMINISTIC,
        json.dumps(dict(s.candidate.payload), sort_keys=True, default=str),
    )


# ---------------------------------------------------------------------------
# Propose-only entry point (unchanged behaviour)
# ---------------------------------------------------------------------------

def propose_for_tenant(tenant_id: str, *, llm_reasoner=None, use_llm: bool = True):
    """Return tenant-scoped candidates only; this function grants no execution."""
    opportunities = detect_opportunities(tenant_id)
    return reason_about_opportunities(
        opportunities, llm_reasoner=llm_reasoner, use_llm=use_llm
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_cycle(
    tenant_id: str,
    *,
    mode: CycleMode = CycleMode.SIMULATE,
    llm_reasoner=None,
    use_llm: bool = True,
    observer: Observer | None = None,
    scorer: Scorer = default_scorer,
    min_score: float = 0.0,
    cycle_id: str | None = None,
    max_opportunities: int = 10,
    max_actions: int = 3,
    max_errors: int = 3,
    cooldown: timedelta = timedelta(hours=24),
    verify_chain: bool = True,
    require_rls: bool = False,
    include_calibration: bool = True,
    now: datetime | None = None,
) -> CycleReport:
    """
    Run one detect -> reason -> simulate -> gate -> act -> observe cycle.

    Defaults are deliberately conservative: mode=SIMULATE performs no writes.
    Pass mode=CycleMode.EXECUTE to act. Pass a stable `cycle_id` when retrying
    a cycle so idempotency keys line up and nothing executes twice.
    """
    tenant_id = validate_tenant_id(tenant_id)
    now = now or datetime.now(timezone.utc)
    cycle_id = cycle_id or str(uuid.uuid4())
    report = CycleReport(cycle_id=cycle_id, tenant_id=tenant_id, mode=mode, started_at=now)

    def finish(status: str, reason: str | None = None) -> CycleReport:
        report.status, report.reason = status, reason
        report.finished_at = datetime.now(timezone.utc)
        _emit(observer, "cycle_finished", tenant_id, cycle_id, status=status,
              reason=reason, actions_attempted=report.actions_attempted)
        return report

    # Stage 0: preflight ----------------------------------------------------
    if require_rls:
        assert_rls_enforced()  # raises PipelineConfigError

    with _tenant_cycle_lock(tenant_id) as acquired:
        if not acquired:
            return finish("skipped", "cycle_already_running")

        _emit(observer, "cycle_started", tenant_id, cycle_id, mode=mode.value)

        if verify_chain:
            try:
                verify_chain_integrity(tenant_id)
            except HashChainCorruptedError as exc:
                logger.error("Event chain corrupted for tenant %s: %s", tenant_id, exc)
                return finish("skipped", "event_chain_corrupted")
            except EventStoreError:
                logger.exception("Event store unavailable for tenant %s", tenant_id)
                return finish("skipped", "event_store_unavailable")

        # Stages 1-2: detect and reason --------------------------------------
        probe = _LLMProbe(llm_reasoner) if (use_llm and llm_reasoner is not None) else None
        try:
            opportunities = detect_opportunities(tenant_id, now=now)[:max_opportunities]
            _emit(observer, "opportunities_detected", tenant_id, cycle_id,
                  count=len(opportunities),
                  ids=[o.id for o in opportunities])
            proposals = reason_about_opportunities(
                opportunities, llm_reasoner=probe or llm_reasoner, use_llm=use_llm
            )
        except Exception as exc:
            logger.exception("Detect/reason failed for tenant %s", tenant_id)
            if probe is not None:
                report.llm = probe.summary()
            return finish("failed", f"{type(exc).__name__}: {exc}")

        if probe is not None:
            report.llm = probe.summary()
            if probe.failures:  # fallback kept the cycle safe; make it visible anyway
                logger.warning("LLM reasoning degraded for tenant %s: %d/%d calls failed: %s",
                               tenant_id, probe.failures, probe.calls, probe.errors[:3])
                _emit(observer, "llm_degraded", tenant_id, cycle_id, **report.llm)

        # Stages 3-6 per opportunity, ranked by |impact| (detector order) ----
        actions_left = max_actions if mode is CycleMode.EXECUTE else 0
        errors = 0
        for opp in opportunities:
            if errors >= max_errors:
                return finish("aborted", f"{errors} errors; remaining opportunities skipped")
            try:
                outcome = _handle(
                    tenant_id, cycle_id, mode, opp, proposals.get(opp.id, []),
                    scorer=scorer, min_score=min_score, cooldown=cooldown, now=now,
                    actions_left=actions_left, observer=observer,
                )
            except Exception as exc:  # contain failures per opportunity
                logger.exception("Opportunity %s failed", opp.id)
                errors += 1
                outcome = OpportunityOutcome(
                    opp.id, opp.opportunity_type.value, opp.estimated_monthly_impact,
                    OutcomeStatus.ERROR, detail=f"{type(exc).__name__}: {exc}",
                )
            if outcome.result is not None:
                actions_left -= 1
            report.outcomes.append(outcome)

        # Observe: calibration snapshot --------------------------------------
        if include_calibration and any(
            o.status is OutcomeStatus.EXECUTED for o in report.outcomes
        ):
            try:
                report.calibration = overall_calibration(tenant_id)
            except Exception:
                logger.exception("Calibration snapshot failed")

        return finish("completed")


def _handle(tenant_id: str, cycle_id: str, mode: CycleMode, opp: Opportunity,
            candidates: list[CandidateAction], *, scorer: Scorer, min_score: float,
            cooldown: timedelta, now: datetime, actions_left: int,
            observer: Observer | None) -> OpportunityOutcome:
    def outcome(status: OutcomeStatus, **kw) -> OpportunityOutcome:
        return OpportunityOutcome(opp.id, opp.opportunity_type.value,
                                  opp.estimated_monthly_impact, status, **kw)

    if not candidates:
        return outcome(OutcomeStatus.NO_CANDIDATES)

    _emit(observer, "candidates_generated", tenant_id, cycle_id,
          opportunity_id=opp.id, count=len(candidates),
          sources=sorted({c.source.value for c in candidates}))

    if mode is CycleMode.PROPOSE:
        return outcome(OutcomeStatus.PROPOSED,
                       candidates=[ScoredCandidate(c, None, None).summary() for c in candidates])

    scored = sorted(_score(tenant_id, candidates, scorer), key=_rank_key)
    summaries = [s.summary() for s in scored]
    viable = [s for s in scored if s.score is not None and s.score > min_score]
    _emit(observer, "candidates_simulated", tenant_id, cycle_id, opportunity_id=opp.id,
          viable=len(viable), failed=sum(1 for s in scored if s.error))

    if not viable:  # "take no action" is a valid decision
        failures = [s.error for s in scored if s.error]
        return outcome(OutcomeStatus.NO_BENEFICIAL_ACTION, candidates=summaries,
                       detail="; ".join(failures) or None)

    review_supported = _forced_review_supported()
    if not review_supported:
        # No way to route a flagged candidate to a reviewer: it must never
        # execute, and it must not starve a safe alternative ranked below it
        # (LLMs echo the prompt template's `"requires_human_review": true`,
        # so flagged candidates will often rank first).
        flagged = [s for s in viable if s.candidate.requires_human_review]
        viable = [s for s in viable if not s.candidate.requires_human_review]
        if not viable:
            return outcome(
                OutcomeStatus.NEEDS_REVIEW_NOT_EXECUTED, candidates=summaries,
                selected=flagged[0].summary(),
                detail="every viable candidate requires human review and tool_framework "
                       "has no require_approval_reason support; nothing executed",
            )

    best = viable[0]
    chosen = best.candidate
    if _in_cooldown(tenant_id, chosen, cooldown, now):
        return outcome(OutcomeStatus.COOLDOWN_ACTIVE, candidates=summaries,
                       selected=best.summary())

    if mode is CycleMode.SIMULATE:
        return outcome(OutcomeStatus.SIMULATED_ONLY, candidates=summaries,
                       selected=best.summary())

    if actions_left <= 0:
        return outcome(OutcomeStatus.ACTION_LIMIT_REACHED, candidates=summaries,
                       selected=best.summary())

    kwargs: dict[str, Any] = {}
    if chosen.requires_human_review:  # only reachable when review_supported
        kwargs["require_approval_reason"] = (
            f"{chosen.source.value} candidate flagged for human review: "
            f"{chosen.rationale[:200]}"
        )

    _emit(observer, "action_attempted", tenant_id, cycle_id, opportunity_id=opp.id,
          action_type=chosen.action_type.value, payload=dict(chosen.payload))
    result = execute_tool_call_idempotent(
        tenant_id, _idempotency_key(cycle_id, opp.id, chosen),
        chosen.action_type, dict(chosen.payload), **kwargs,
    )
    if result.get("executed"):
        status = OutcomeStatus.EXECUTED
    else:
        try:
            status = OutcomeStatus(result.get("reason"))
        except ValueError:
            status = OutcomeStatus.ERROR
    _emit(observer, "action_result", tenant_id, cycle_id, opportunity_id=opp.id,
          status=status.value, decision_id=result.get("decision_id"),
          calibration_error=result.get("calibration_error"))
    return outcome(status, candidates=summaries, selected=best.summary(), result=result)


# ---------------------------------------------------------------------------
# Human review step
# ---------------------------------------------------------------------------

def _stale_approval_reason(tenant_id: str, decision_id: str) -> str | None:
    """
    Re-run the critic against CURRENT state for a decision awaiting approval.
    Narrows (does not close) the window between approval and execution; the
    complete fix is to do this inside tool_framework under the tenant lock.
    """
    with get_db_context(tenant_id) as session:
        row = (
            session.query(Decision.action_type, Decision.action_payload)
            .filter(Decision.id == decision_id, Decision.tenant_id == tenant_id)
            .one_or_none()
        )
    if row is None:
        return "approved decision not found for tenant"
    action_type, payload = row[0], dict(row[1])
    verdict = critique(action_type, payload,
                       _current_value_for_critic(tenant_id, action_type, payload))
    return None if verdict.approved else verdict.reason


def resolve_approval(
    tenant_id: str,
    approval_request_id: str,
    *,
    approved: bool,
    reviewer_id: str,
    override_reason: str | None = None,
    observer: Observer | None = None,
) -> dict[str, Any]:
    """
    Human-in-the-loop step. Approve -> re-validate against live state ->
    execute exactly once. Reject -> record and stop.

    A request that no longer passes the critic is NOT executed and is left
    pending, so the reviewer can reject it or ask for a fresh proposal.
    """
    tenant_id = validate_tenant_id(tenant_id)
    approval = approval_workflow.get(tenant_id, approval_request_id)  # KeyError if not ours
    if approval.status is not ApprovalStatus.PENDING:
        raise ValueError("approval request has already been resolved")

    if approved:
        stale = _stale_approval_reason(tenant_id, approval.decision_id)
        if stale:
            _emit(observer, "approval_stale", tenant_id, None,
                  approval_request_id=approval_request_id, critic_reason=stale)
            return {"executed": False, "reason": "stale_approval",
                    "critic_reason": stale, "approval_request_id": approval_request_id}

    approval_workflow.resolve(
        tenant_id, approval_request_id,
        approved=approved, reviewer_id=reviewer_id, override_reason=override_reason,
    )
    _emit(observer, "approval_resolved", tenant_id, None,
          approval_request_id=approval_request_id, approved=approved,
          reviewer_id=reviewer_id)

    if not approved:
        return {"executed": False, "reason": "rejected",
                "approval_request_id": approval_request_id}

    result = execute_approved_tool_call(tenant_id, approval_request_id)
    _emit(observer, "approval_executed", tenant_id, None,
          approval_request_id=approval_request_id,
          decision_id=result.get("decision_id"),
          calibration_error=result.get("calibration_error"))
    return result