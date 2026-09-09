"""
Production-grade tests for ground_truth.py.

These tests validate the synthetic environment's contract rather than
claiming that its generated values represent real restaurant behavior.
"""

from copy import replace
import math

import pytest

from Component_2.ground_truth import (
    MAX_STOCKOUT_PROBABILITY,
    ActualOutcome,
    observe_actual_outcome,
)
from Component_1.models import ActionType
from Component_2.simulator import PredictedOutcome


class DeterministicRNG:
    """Deterministic RNG used to test specific environment branches."""

    def __init__(
        self,
        *,
        gauss_values=None,
        random_values=None,
        uniform_values=None,
        choice_values=None,
    ):
        self.gauss_values = iter(gauss_values or [0.0])
        self.random_values = iter(random_values or [1.0])
        self.uniform_values = iter(uniform_values or [0.10])
        self.choice_values = iter(choice_values or [1])

    def gauss(self, mu, sigma):
        return next(self.gauss_values)

    def random(self):
        return next(self.random_values)

    def uniform(self, a, b):
        return next(self.uniform_values)

    def choice(self, sequence):
        return next(self.choice_values)


@pytest.fixture
def predicted():
    return PredictedOutcome(
        margin_change_pct=0.10,
        demand_change_pct=-0.05,
        stockout_risk=0.10,
        confidence=0.8,
        rationale="test prediction",
    )


def test_returns_actual_outcome(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 1.0],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {"menu_item_id": "item-1", "new_price": 13.0},
        predicted,
        rng=rng,
    )

    assert isinstance(result, ActualOutcome)
    assert result.margin_change_pct == 0.10
    assert result.demand_change_pct == -0.05
    assert result.stockout_occurred is False


def test_to_dict_returns_serializable_mapping(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 1.0],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    data = result.to_dict()

    assert data == {
        "margin_change_pct": 0.10,
        "demand_change_pct": -0.05,
        "stockout_occurred": False,
    }


def test_small_noise_changes_observation(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.02, -0.03],
        random_values=[1.0, 1.0],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert result.margin_change_pct == 0.12
    assert result.demand_change_pct == -0.08


def test_surprise_event_introduces_systematic_miss(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[0.0, 1.0],
        uniform_values=[0.20],
        choice_values=[1],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert result.margin_change_pct == 0.30
    assert result.demand_change_pct == 0.05


def test_negative_surprise_is_supported(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[0.0, 1.0],
        uniform_values=[0.08],
        choice_values=[-1],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert result.margin_change_pct == 0.02
    assert result.demand_change_pct == -0.09


def test_high_stockout_risk_can_produce_stockout(predicted):
    predicted = replace(predicted, stockout_risk=0.9)
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 0.0],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert result.stockout_occurred is True


def test_zero_stockout_risk_without_surprise_does_not_force_stockout(predicted):
    predicted = replace(predicted, margin_change_pct=0.0)
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 0.99],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert result.stockout_occurred is False


def test_stockout_probability_is_bounded(predicted):
    predicted = replace(predicted, margin_change_pct=1.0)
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[0.0, 0.0],
        uniform_values=[0.20],
        choice_values=[1],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert isinstance(result.stockout_occurred, bool)
    assert 0.0 <= MAX_STOCKOUT_PROBABILITY <= 1.0


def test_rejects_invalid_action_type(predicted):
    with pytest.raises(
        TypeError,
        match="action_type must be an ActionType",
    ):
        observe_actual_outcome(
            "PRICE_CHANGE",
            {},
            predicted,
        )


def test_rejects_invalid_payload(predicted):
    with pytest.raises(
        TypeError,
        match="payload must be a mapping",
    ):
        observe_actual_outcome(
            ActionType.PRICE_CHANGE,
            None,
            predicted,
        )


def test_rejects_none_prediction():
    with pytest.raises(
        ValueError,
        match="predicted outcome must not be None",
    ):
        observe_actual_outcome(
            ActionType.PRICE_CHANGE,
            {},
            None,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("margin_change_pct", float("nan")),
        ("margin_change_pct", float("inf")),
        ("margin_change_pct", float("-inf")),
        ("demand_change_pct", float("nan")),
        ("demand_change_pct", float("inf")),
        ("demand_change_pct", float("-inf")),
        ("stockout_risk", float("nan")),
        ("stockout_risk", float("inf")),
        ("stockout_risk", float("-inf")),
    ],
)
def test_rejects_non_finite_prediction(field, value, predicted):
    # predicted = _make_prediction()

    object.__setattr__(predicted, field, value)
    with pytest.raises(
        ValueError,
        match="must be finite",
    ):
        observe_actual_outcome(
            ActionType.PRICE_CHANGE,
            {},
            predicted,
        )


@pytest.mark.parametrize(
    "stockout_risk",
    [-0.01, 1.01, 2.0],
)
def test_rejects_invalid_stockout_probability(
    stockout_risk,
    predicted,
):
    object.__setattr__(
        predicted,
        "stockout_risk",
        stockout_risk,
    )

    with pytest.raises(
        ValueError,
        match="must be between 0 and 1",
    ):
        observe_actual_outcome(
            ActionType.PRICE_CHANGE,
            {},
            predicted,
        )


@pytest.mark.parametrize(
    "action_type",
    list(ActionType),
)
def test_all_supported_action_types_are_accepted(
    action_type,
    predicted,
):
    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 1.0],
    )

    result = observe_actual_outcome(
        action_type,
        {},
        predicted,
        rng=rng,
    )

    assert isinstance(result, ActualOutcome)


def test_generated_values_are_finite(predicted):
    rng = DeterministicRNG(
        gauss_values=[0.01, -0.02],
        random_values=[1.0, 1.0],
    )

    result = observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        {},
        predicted,
        rng=rng,
    )

    assert math.isfinite(result.margin_change_pct)
    assert math.isfinite(result.demand_change_pct)


def test_payload_is_not_mutated(predicted):
    payload = {
        "menu_item_id": "item-1",
        "new_price": 13.0,
    }
    original = payload.copy()

    rng = DeterministicRNG(
        gauss_values=[0.0, 0.0],
        random_values=[1.0, 1.0],
    )

    observe_actual_outcome(
        ActionType.PRICE_CHANGE,
        payload,
        predicted,
        rng=rng,
    )

    assert payload == original