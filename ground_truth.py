"""
Ground-truth / environment model — used ONLY to generate a synthetic
"actual outcome" for a decision in this demo, so calibration can be
measured without needing a real restaurant's live results.

This is deliberately kept SEPARATE from simulator.py's prediction logic —
if the same function both predicted and "observed" the outcome, calibration
would trivially be perfect, which would prove nothing. This module adds
its own independent (and noisier, sometimes-wrong-on-purpose) model of
reality, standing in for what would eventually be real observed data from
a deployed restaurant.

In a real deployment, this entire module goes away — actual_outcome would
come from real sales/cost data after the action executes, not from a
second simulator.

This module generates a synthetic "actual" outcome for a decision so the
Component 2 calibration loop can be exercised without real restaurant data.

IMPORTANT:
    This is NOT real ground truth and is NOT a trained model.

    simulator.py predicts an outcome using explicit heuristics.
    This module independently generates a noisy synthetic observation.

    In production, this module should eventually be replaced by observations
    derived from real post-execution restaurant data such as sales, ingredient
    costs, inventory, and stockout events.

Design principles:
    - Independent from simulator prediction logic.
    - Explicitly typed inputs and outputs.
    - Defensive validation of prediction values.
    - Injectable RNG for deterministic tests.
    - Bounded outputs to prevent invalid probabilities.
    - No database access: tenant isolation belongs to the decision/data layer.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Protocol

from models import ActionType
from simulator import (
    ASSUMED_FOOD_COST_RATIO,
    PRICE_ELASTICITY,
)


# ---------------------------------------------------------------------------
# Synthetic environment assumptions
# ---------------------------------------------------------------------------

BASE_MARGIN_NOISE_STDDEV = 0.03
BASE_DEMAND_NOISE_STDDEV = 0.04

SURPRISE_PROBABILITY = 0.15
SURPRISE_MIN_MAGNITUDE = 0.08
SURPRISE_MAX_MAGNITUDE = 0.20

SURPRISE_STOCKOUT_BONUS = 0.05
MAX_STOCKOUT_PROBABILITY = 0.95


class RandomSource(Protocol):
    """Minimal RNG interface required by the synthetic environment."""

    def gauss(self, mu: float, sigma: float) -> float:
        """Return a Gaussian random value."""

    def random(self) -> float:
        """Return a random value in [0.0, 1.0)."""

    def uniform(self, a: float, b: float) -> float:
        """Return a random value between a and b."""

    def choice(self, sequence: list[int]) -> int:
        """Return one value from a sequence."""


@dataclass(frozen=True)
class ActualOutcome:
    """
    Synthetic post-execution observation.

    Values represent relative changes rather than absolute restaurant
    financial values.
    """

    margin_change_pct: float
    demand_change_pct: float
    stockout_occurred: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation of the observation."""
        return asdict(self)


def _validate_finite_number(
    value: Any,
    field_name: str,
) -> float:
    """
    Validate and normalize a numeric value.

    Rejects:
        - None
        - booleans
        - strings that cannot be converted to float
        - NaN
        - positive infinity
        - negative infinity
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be numeric"
        ) from exc

    if not math.isfinite(numeric_value):
        raise ValueError(
            f"{field_name} must be finite"
        )

    return numeric_value


def _validate_action_type(action_type: ActionType) -> None:
    """Ensure the caller supplies a valid action type."""
    if not isinstance(action_type, ActionType):
        raise TypeError("action_type must be an ActionType")


def _validate_payload(payload: Mapping[str, Any]) -> None:
    """
    Validate the action payload boundary.

    The synthetic environment does not currently interpret payload fields,
    but requiring a mapping prevents accidental API misuse and keeps the
    interface compatible with the decision engine.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")


def _validate_prediction(predicted: Any) -> None:
    """
    Validate the prediction consumed by the ground-truth model.

    The prediction must expose finite margin, demand, and stockout-risk
    values. This prevents NaN/infinity from contaminating calibration.
    """
    if predicted is None:
        raise ValueError("predicted outcome must not be None")

    margin = _validate_finite_number(
        predicted.margin_change_pct,
        "predicted.margin_change_pct",
    )

    demand = _validate_finite_number(
        predicted.demand_change_pct,
        "predicted.demand_change_pct",
    )

    stockout_risk = _validate_finite_number(
        predicted.stockout_risk,
        "predicted.stockout_risk",
    )

    if not 0.0 <= stockout_risk <= 1.0:
        raise ValueError(
            "predicted.stockout_risk must be between 0 and 1"
        )

    # Explicitly validate these constants as well. This makes configuration
    # corruption fail closed rather than silently generating bad observations.
    _validate_finite_number(
        PRICE_ELASTICITY,
        "PRICE_ELASTICITY",
    )

    _validate_finite_number(
        ASSUMED_FOOD_COST_RATIO,
        "ASSUMED_FOOD_COST_RATIO",
    )

    # Keep local variables referenced so static analysis recognizes that the
    # prediction fields were deliberately validated.
    _ = margin, demand


def _bounded_stockout_probability(
    predicted_risk: float,
    surprise: bool,
) -> float:
    """Return a valid probability in [0, MAX_STOCKOUT_PROBABILITY]."""
    bonus = SURPRISE_STOCKOUT_BONUS if surprise else 0.0

    probability = predicted_risk + bonus

    if not math.isfinite(probability):
        raise ValueError("stockout probability is not finite")

    return max(
        0.0,
        min(probability, MAX_STOCKOUT_PROBABILITY),
    )


def observe_actual_outcome(
    action_type: ActionType,
    payload: Mapping[str, Any],
    predicted: Any,
    *,
    rng: RandomSource | None = None,
) -> ActualOutcome:
    """
    Generate an independent synthetic observation.

    Args:
        action_type:
            Action being evaluated.
        payload:
            Original action payload. It is validated but intentionally not
            interpreted by this synthetic model.
        predicted:
            Previously generated simulator prediction.
        rng:
            Optional injectable random source. Supplying one makes tests
            deterministic without globally monkeypatching random behavior.

    Returns:
        ActualOutcome containing noisy synthetic observations.

    Raises:
        TypeError:
            If action_type or payload has an invalid type.
        ValueError:
            If prediction values are invalid or generated output is non-finite.
    """
    _validate_action_type(action_type)
    _validate_payload(payload)
    _validate_prediction(predicted)

    source = rng if rng is not None else random

    # Small ordinary deviation.
    margin_noise = source.gauss(
        0.0,
        BASE_MARGIN_NOISE_STDDEV,
    )

    demand_noise = source.gauss(
        0.0,
        BASE_DEMAND_NOISE_STDDEV,
    )

    # Occasionally introduce a larger unmodeled environmental effect.
    surprise = source.random() < SURPRISE_PROBABILITY

    if surprise:
        surprise_magnitude = (
            source.uniform(
                SURPRISE_MIN_MAGNITUDE,
                SURPRISE_MAX_MAGNITUDE,
            )
            * source.choice([-1, 1])
        )
    else:
        surprise_magnitude = 0.0

    actual_margin = (
        float(predicted.margin_change_pct)
        + margin_noise
        + surprise_magnitude
    )

    actual_demand = (
        float(predicted.demand_change_pct)
        + demand_noise
        + surprise_magnitude * 0.5
    )

    stockout_probability = _bounded_stockout_probability(
        float(predicted.stockout_risk),
        surprise,
    )

    stockout_occurred = source.random() < stockout_probability

    if not math.isfinite(actual_margin):
        raise ValueError("generated actual margin is not finite")

    if not math.isfinite(actual_demand):
        raise ValueError("generated actual demand is not finite")

    return ActualOutcome(
        margin_change_pct=round(actual_margin, 4),
        demand_change_pct=round(actual_demand, 4),
        stockout_occurred=stockout_occurred,
    )