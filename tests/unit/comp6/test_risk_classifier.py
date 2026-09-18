"""
Exhaustive unit tests for Component 6 risk classification.

Properties under test
---------------------
1. Pure function:
   - no database access
   - no network calls
   - no mutation of caller-owned payloads
   - deterministic for identical inputs

2. Fail closed:
   - unknown action -> HIGH + approval
   - invalid payload -> HIGH + approval
   - invalid numeric values -> HIGH + approval
   - invalid/missing confidence -> HIGH + approval
   - low confidence -> HIGH + approval

3. Price policy:
   - <= 10% -> LOW / autonomous
   - > 10% and <= critic hard limit -> MEDIUM / approval
   - > critic hard limit -> HIGH / approval

4. Supplier switch:
   - valid request -> MEDIUM / approval
   - invalid numeric input -> HIGH / approval

5. Menu removal:
   - always HIGH / approval
   - non-removal / unsupported menu action -> HIGH / approval

6. Staffing:
   - valid numeric delta -> MEDIUM / approval
   - invalid numeric delta -> HIGH / approval

7. Result integrity:
   - RiskClassification is frozen
   - correct enum type
   - reversible semantics are explicit
   - reason is non-empty
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace

import pytest

from Component_1.models import ActionType
from Component_4.critic import (
    MAX_PRICE_CHANGE_PCT,
    _is_valid_numeric,
)
from Component_6.risk_classifier import (
    MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION,
    RiskClassification,
    RiskLevel,
    classify_risk,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def prediction(confidence=0.95):
    """
    Construct the smallest prediction-shaped object accepted by the current
    classifier.

    If Component 2 exposes a concrete Decision/PredictedOutcome constructor,
    replace this helper with that constructor so these tests exercise the
    exact production object.
    """
    return SimpleNamespace(confidence=confidence)


def assert_high(result: RiskClassification):
    assert isinstance(result, RiskClassification)
    assert result.risk_level is RiskLevel.HIGH
    assert result.requires_approval is True
    assert isinstance(result.reason, str)
    assert result.reason.strip()


def assert_medium(result: RiskClassification):
    assert isinstance(result, RiskClassification)
    assert result.risk_level is RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert isinstance(result.reason, str)
    assert result.reason.strip()


def assert_low(result: RiskClassification):
    assert isinstance(result, RiskClassification)
    assert result.risk_level is RiskLevel.LOW
    assert result.requires_approval is False
    assert result.reversible is True
    assert isinstance(result.reason, str)
    assert result.reason.strip()


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------


class TestRiskClassification:
    def test_is_frozen(self):
        result = RiskClassification(
            risk_level=RiskLevel.LOW,
            reversible=True,
            requires_approval=False,
            reason="test",
        )

        with pytest.raises(FrozenInstanceError):
            result.risk_level = RiskLevel.HIGH

    def test_has_exact_required_fields(self):
        result = RiskClassification(
            risk_level=RiskLevel.LOW,
            reversible=True,
            requires_approval=False,
            reason="test",
        )

        assert result.risk_level is RiskLevel.LOW
        assert result.reversible is True
        assert result.requires_approval is False
        assert result.reason == "test"

    def test_risk_level_is_enum(self):
        assert set(RiskLevel) == {
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
        }

    def test_enum_values_are_stable(self):
        assert RiskLevel.LOW.value == "LOW"
        assert RiskLevel.MEDIUM.value == "MEDIUM"
        assert RiskLevel.HIGH.value == "HIGH"


# ---------------------------------------------------------------------------
# Pure-function / side-effect properties
# ---------------------------------------------------------------------------


class TestPurity:
    def test_same_input_produces_same_result(self):
        payload = {"new_price": 105.0}

        first = classify_risk(
            ActionType.PRICE_CHANGE,
            payload,
            prediction(0.95),
            100.0,
        )

        second = classify_risk(
            ActionType.PRICE_CHANGE,
            payload,
            prediction(0.95),
            100.0,
        )

        assert first == second

    def test_does_not_mutate_payload(self):
        payload = {
            "menu_item_id": "menu-1",
            "new_price": 105.0,
            "metadata": {"source": "test"},
        }

        original = payload.copy()
        original["metadata"] = original["metadata"].copy()

        classify_risk(
            ActionType.PRICE_CHANGE,
            payload,
            prediction(0.95),
            100.0,
        )

        assert payload == original

    def test_does_not_mutate_nested_payload(self):
        metadata = {"nested": {"important": True}}
        payload = {
            "new_price": 105.0,
            "metadata": metadata,
        }

        before = repr(payload)

        classify_risk(
            ActionType.PRICE_CHANGE,
            payload,
            prediction(0.95),
            100.0,
        )

        assert repr(payload) == before

    def test_does_not_require_database(self, monkeypatch):
        """
        If the classifier accidentally starts querying Component 1, this
        test should fail.

        Patch common DB entry points to explode if touched.
        """

        def fail_if_called(*args, **kwargs):
            raise AssertionError("Risk classifier accessed the database")

        monkeypatch.setattr(
            "Component_1.db.get_db_context",
            fail_if_called,
            raising=False,
        )

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.95),
            100.0,
        )

        assert_low(result)

    def test_does_not_call_critic_policy_function(self, monkeypatch):
        """
        Component 6 may reuse critic numeric helpers, but should not invoke
        the critic as a second policy engine.
        """

        def fail_if_called(*args, **kwargs):
            raise AssertionError("risk_classifier called critique()")

        monkeypatch.setattr(
            "Component_6.risk_classifier.critique",
            fail_if_called,
            raising=False,
        )

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.95),
            100.0,
        )

        assert_low(result)


# ---------------------------------------------------------------------------
# Universal confidence gate
# ---------------------------------------------------------------------------


class TestConfidence:
    @pytest.mark.parametrize(
        "confidence",
        [
            0.0,
            0.01,
            0.10,
            0.50,
            0.79,
            MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION - 0.000001,
        ],
    )
    def test_low_confidence_is_high_risk(self, confidence):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(confidence),
            100.0,
        )

        assert_high(result)
        assert "confidence" in result.reason.lower()

    def test_confidence_exactly_at_threshold_is_allowed(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION),
            100.0,
        )

        assert_low(result)

    @pytest.mark.parametrize(
        "confidence",
        [
            0.800000001,
            0.90,
            0.99,
            1.0,
        ],
    )
    def test_high_confidence_is_accepted(self, confidence):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(confidence),
            100.0,
        )

        assert_low(result)

    @pytest.mark.parametrize(
        "confidence",
        [
            None,
            "0.95",
            "",
            object(),
            True,
            False,
        ],
    )
    def test_invalid_confidence_fails_closed(self, confidence):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(confidence),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "confidence",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_non_finite_confidence_fails_closed(self, confidence):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(confidence),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "confidence",
        [
            -0.000001,
            -0.1,
            -1.0,
            1.000001,
            2.0,
            100.0,
        ],
    )
    def test_out_of_range_confidence_fails_closed(self, confidence):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(confidence),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "predicted",
        [
            None,
            {},
            object(),
            SimpleNamespace(),
            SimpleNamespace(confidence=None),
        ],
    )
    def test_invalid_prediction_object_fails_closed(self, predicted):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            predicted,
            100.0,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# Action-type validation
# ---------------------------------------------------------------------------


class TestActionTypeValidation:
    @pytest.mark.parametrize(
        "action_type",
        [
            None,
            "",
            "PRICE_CHANGE",
            "price_change",
            "delete_database",
            123,
            True,
            object(),
        ],
    )
    def test_unrecognized_action_type_is_high(self, action_type):
        result = classify_risk(
            action_type,
            {},
            prediction(0.99),
            100.0,
        )

        assert_high(result)
        assert "action" in result.reason.lower()

    def test_enum_subclass_like_object_is_not_accepted(self):
        fake = SimpleNamespace(value="PRICE_CHANGE")

        result = classify_risk(
            fake,
            {"new_price": 105.0},
            prediction(0.99),
            100.0,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            (),
            "",
            "payload",
            123,
            1.5,
            True,
            object(),
        ],
    )
    def test_non_dict_payload_fails_closed(self, payload):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            payload,
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    def test_empty_payload_for_price_change_fails_closed(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    def test_empty_payload_for_supplier_switch_fails_closed(self):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    def test_empty_payload_for_staffing_fails_closed(self):
        result = classify_risk(
            ActionType.STAFFING_CHANGE,
            {},
            prediction(0.99),
            100.0,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# PRICE_CHANGE — LOW risk
# ---------------------------------------------------------------------------


class TestPriceChangeLowRisk:
    def test_zero_percent_change_is_low(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 100.0},
            prediction(0.99),
            100.0,
        )

        assert_low(result)

    @pytest.mark.parametrize(
        "new_price",
        [
            100.01,
            101.0,
            105.0,
            109.999,
            110.0,
        ],
    )
    def test_small_price_changes_are_low(self, new_price):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_low(result)

    def test_small_price_decrease_is_low(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 90.0},
            prediction(0.99),
            100.0,
        )

        assert_low(result)

    def test_small_price_change_is_reversible(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.99),
            100.0,
        )

        assert result.reversible is True
        assert result.requires_approval is False

    def test_exactly_ten_percent_is_low(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 110.0},
            prediction(0.99),
            100.0,
        )

        assert result.risk_level is RiskLevel.LOW


# ---------------------------------------------------------------------------
# PRICE_CHANGE — MEDIUM risk
# ---------------------------------------------------------------------------


class TestPriceChangeMediumRisk:
    @pytest.mark.parametrize(
        "new_price",
        [
            110.001,
            115.0,
            120.0,
            125.0,
            129.999,
            130.0,
        ],
    )
    def test_larger_valid_price_changes_are_medium(self, new_price):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_medium(result)

    @pytest.mark.parametrize(
        "new_price",
        [
            89.999,
            85.0,
            80.0,
            70.001,
            70.0,
        ],
    )
    def test_larger_price_decreases_within_hard_limit_are_medium(
        self,
        new_price,
    ):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_medium(result)

    def test_medium_price_change_requires_approval(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 120.0},
            prediction(0.99),
            100.0,
        )

        assert result.requires_approval is True

    def test_medium_price_change_remains_reversible(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 120.0},
            prediction(0.99),
            100.0,
        )

        assert result.reversible is True


# ---------------------------------------------------------------------------
# PRICE_CHANGE — HIGH / hard-limit violations
# ---------------------------------------------------------------------------


class TestPriceChangeHighRisk:
    def test_change_above_critic_hard_limit_is_high(self):
        new_price = 100.0 * (1 + MAX_PRICE_CHANGE_PCT + 0.000001)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(1.0),
            100.0,
        )

        assert_high(result)

    def test_large_price_decrease_beyond_hard_limit_is_high(self):
        new_price = 100.0 * (1 - MAX_PRICE_CHANGE_PCT - 0.000001)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(1.0),
            100.0,
        )

        assert_high(result)

    def test_exact_hard_limit_is_not_high(self):
        new_price = 100.0 * (1 + MAX_PRICE_CHANGE_PCT)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(1.0),
            100.0,
        )

        assert result.risk_level is RiskLevel.MEDIUM

    def test_high_confidence_does_not_override_hard_limit(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 200.0},
            prediction(1.0),
            100.0,
        )

        assert_high(result)
        assert result.requires_approval is True


# ---------------------------------------------------------------------------
# PRICE_CHANGE — numeric attacks
# ---------------------------------------------------------------------------


class TestPriceChangeNumericRedTeam:
    @pytest.mark.parametrize(
        "new_price",
        [
            None,
            "105",
            "105.00",
            "",
            "NaN",
            "Infinity",
            [],
            {},
            object(),
            True,
            False,
        ],
    )
    def test_malformed_new_price_fails_closed(self, new_price):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "new_price",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_non_finite_new_price_fails_closed(self, new_price):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "current_value",
        [
            None,
            0,
            0.0,
            -1,
            -100,
            "100",
            True,
            False,
            object(),
        ],
    )
    def test_invalid_current_value_fails_closed(self, current_value):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.99),
            current_value,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "current_value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_non_finite_current_value_fails_closed(self, current_value):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.99),
            current_value,
        )

        assert_high(result)

    def test_decimal_price_values_are_supported(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": Decimal("105.00")},
            prediction(0.99),
            Decimal("100.00"),
        )

        assert_low(result)

    def test_integer_price_values_are_supported(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105},
            prediction(0.99),
            100,
        )

        assert_low(result)


# ---------------------------------------------------------------------------
# SUPPLIER_SWITCH
# ---------------------------------------------------------------------------


class TestSupplierSwitch:
    def test_supplier_switch_is_medium(self):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 110.0},
            prediction(0.99),
            100.0,
        )

        assert_medium(result)

    def test_supplier_switch_requires_approval(self):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 101.0},
            prediction(1.0),
            100.0,
        )

        assert result.requires_approval is True

    def test_supplier_switch_is_reversible(self):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 101.0},
            prediction(1.0),
            100.0,
        )

        assert result.reversible is True

    @pytest.mark.parametrize(
        "new_price",
        [
            None,
            "100",
            "",
            True,
            False,
            object(),
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_invalid_supplier_price_is_high(self, new_price):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": new_price},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

    @pytest.mark.parametrize(
        "current_value",
        [
            None,
            0,
            -1,
            "100",
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_invalid_supplier_current_value_is_high(self, current_value):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 110.0},
            prediction(0.99),
            current_value,
        )

        assert_high(result)

    def test_missing_supplier_price_is_high(self):
        result = classify_risk(
            ActionType.SUPPLIER_SWITCH,
            {},
            prediction(0.99),
            100.0,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# MENU_SWAP
# ---------------------------------------------------------------------------


class TestMenuSwap:
    def test_menu_removal_is_high(self):
        result = classify_risk(
            ActionType.MENU_SWAP,
            {
                "action": "remove",
                "menu_item_id": "menu-1",
            },
            prediction(1.0),
            None,
        )

        assert_high(result)

    def test_menu_removal_is_not_autonomous(self):
        result = classify_risk(
            ActionType.MENU_SWAP,
            {
                "action": "remove",
                "menu_item_id": "menu-1",
            },
            prediction(1.0),
            None,
        )

        assert result.requires_approval is True

    def test_menu_removal_is_not_reversible(self):
        result = classify_risk(
            ActionType.MENU_SWAP,
            {
                "action": "remove",
                "menu_item_id": "menu-1",
            },
            prediction(1.0),
            None,
        )

        assert result.reversible is False

    @pytest.mark.parametrize(
        "action",
        [
            None,
            "",
            "add",
            "ADD",
            "delete",
            "replace",
            "swap",
            123,
            True,
            False,
        ],
    )
    def test_unsupported_menu_action_is_high(self, action):
        result = classify_risk(
            ActionType.MENU_SWAP,
            {"action": action},
            prediction(1.0),
            None,
        )

        assert_high(result)

    def test_missing_menu_action_is_high(self):
        result = classify_risk(
            ActionType.MENU_SWAP,
            {},
            prediction(1.0),
            None,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# STAFFING_CHANGE
# ---------------------------------------------------------------------------


class TestStaffingChange:
    @pytest.mark.parametrize(
        "delta",
        [
            -5,
            -1,
            0,
            1,
            5,
            5.0,
            Decimal("5"),
        ],
    )
    def test_valid_staffing_change_is_medium(self, delta):
        result = classify_risk(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
            prediction(0.99),
            None,
        )

        assert_medium(result)

    @pytest.mark.parametrize(
        "delta",
        [
            None,
            "1",
            "5",
            "five",
            True,
            False,
            object(),
            1.5,
            -2.5,
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("1.5"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_invalid_staffing_delta_is_high(self, delta):
        result = classify_risk(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
            prediction(0.99),
            None,
        )

        assert_high(result)

    def test_missing_staffing_delta_is_high(self):
        result = classify_risk(
            ActionType.STAFFING_CHANGE,
            {},
            prediction(0.99),
            None,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# Confidence must dominate action risk
# ---------------------------------------------------------------------------


class TestConfidenceDominatesRisk:
    """
    Red-team invariant:

        low confidence + low-impact action
            => HIGH

        low confidence + medium-impact action
            => HIGH

        low confidence + high-impact action
            => HIGH

    The classifier must never downgrade risk because the action itself is
    normally safe.
    """

    @pytest.mark.parametrize(
        "action_type,payload,current_value",
        [
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 101.0},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 120.0},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": 101.0},
                100.0,
            ),
            (
                ActionType.MENU_SWAP,
                {"action": "remove"},
                None,
            ),
            (
                ActionType.STAFFING_CHANGE,
                {"headcount_delta": 1},
                None,
            ),
        ],
    )
    def test_low_confidence_never_downgrades_risk(
        self,
        action_type,
        payload,
        current_value,
    ):
        result = classify_risk(
            action_type,
            payload,
            prediction(0.10),
            current_value,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# Universal fail-closed matrix
# ---------------------------------------------------------------------------


class TestFailClosedMatrix:
    """
    Every combination below must fail closed.

    This is intentionally redundant with the individual tests. Redundancy
    here is valuable: this matrix acts as a policy invariant if a future
    refactor changes individual branches.
    """

    @pytest.mark.parametrize(
        "action_type,payload,current_value",
        [
            (
                ActionType.PRICE_CHANGE,
                {"new_price": None},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": float("nan")},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": float("inf")},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": "100"},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 100.0},
                None,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 100.0},
                0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 100.0},
                -100,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": None},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": float("nan")},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": "100"},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": 100.0},
                None,
            ),
            (
                ActionType.STAFFING_CHANGE,
                {"headcount_delta": None},
                None,
            ),
            (
                ActionType.STAFFING_CHANGE,
                {"headcount_delta": "5"},
                None,
            ),
            (
                ActionType.STAFFING_CHANGE,
                {},
                None,
            ),
            (
                ActionType.MENU_SWAP,
                {},
                None,
            ),
            (
                ActionType.MENU_SWAP,
                {"action": "add"},
                None,
            ),
        ],
    )
    def test_malformed_inputs_are_always_high(
        self,
        action_type,
        payload,
        current_value,
    ):
        result = classify_risk(
            action_type,
            payload,
            prediction(0.99),
            current_value,
        )

        assert_high(result)


# ---------------------------------------------------------------------------
# Numeric helper reuse
# ---------------------------------------------------------------------------


class TestNumericValidationContract:
    """
    These tests lock Component 6 to Component 4's established numeric
    contract rather than allowing risk_classifier.py to silently develop
    different numeric semantics.
    """

    @pytest.mark.parametrize(
        "value",
        [
            0,
            1,
            -1,
            1.5,
            -1.5,
            Decimal("1.5"),
            Decimal("-1.5"),
        ],
    )
    def test_critic_numeric_helper_accepts_expected_numeric_types(self, value):
        assert _is_valid_numeric(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            None,
            "1",
            "1.5",
            object(),
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_critic_numeric_helper_rejects_unsafe_values(self, value):
        assert _is_valid_numeric(value) is False


# ---------------------------------------------------------------------------
# Policy monotonicity
# ---------------------------------------------------------------------------


class TestPolicyMonotonicity:
    """
    Red-team tests for accidental risk downgrades.

    Increasing the magnitude of a price change must never turn a MEDIUM
    decision into LOW.

    Lowering confidence must never turn HIGH into MEDIUM or LOW.
    """

    def test_price_risk_does_not_decrease_as_change_grows(self):
        percentages = [0.0, 0.05, 0.10, 0.11, 0.20, 0.30, 0.31, 0.50]

        ranks = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
        }

        previous_rank = -1

        for pct in percentages:
            result = classify_risk(
                ActionType.PRICE_CHANGE,
                {"new_price": 100.0 * (1 + pct)},
                prediction(1.0),
                100.0,
            )

            current_rank = ranks[result.risk_level]

            assert current_rank >= previous_rank, (
                f"Risk decreased at {pct:.1%}: "
                f"{result.risk_level}"
            )

            previous_rank = current_rank

    def test_lower_confidence_never_reduces_risk(self):
        confidences = [1.0, 0.95, 0.90, 0.80, 0.79, 0.50, 0.0]

        ranks = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
        }

        previous_rank = -1

        for confidence in confidences:
            result = classify_risk(
                ActionType.PRICE_CHANGE,
                {"new_price": 105.0},
                prediction(confidence),
                100.0,
            )

            current_rank = ranks[result.risk_level]

            assert current_rank >= previous_rank, (
                f"Risk decreased as confidence decreased to "
                f"{confidence}: {result.risk_level}"
            )

            previous_rank = current_rank


# ---------------------------------------------------------------------------
# Boundary tests using multiple baselines
# ---------------------------------------------------------------------------


class TestPercentageBoundaries:
    @pytest.mark.parametrize(
        "current_value",
        [
            1,
            10,
            99.99,
            100,
            1000,
            Decimal("100.00"),
        ],
    )
    def test_ten_percent_boundary_is_consistent(self, current_value):
        new_price = current_value * Decimal("1.10") if isinstance(
            current_value,
            Decimal,
        ) else Decimal(str(current_value)) * Decimal("1.10")

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(1.0),
            current_value,
        )

        assert result.risk_level is RiskLevel.LOW

    @pytest.mark.parametrize(
        "current_value",
        [
            1,
            10,
            100,
            1000,
            Decimal("100.00"),
        ],
    )
    def test_hard_limit_boundary_is_consistent(self, current_value):
        if isinstance(current_value, Decimal):
            new_price = current_value * Decimal("1.30")
        else:
            new_price = current_value * 1.30

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            prediction(1.0),
            current_value,
        )

        assert result.risk_level is RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Reason quality
# ---------------------------------------------------------------------------


class TestReasons:
    @pytest.mark.parametrize(
        "action_type,payload,current_value",
        [
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 105.0},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 120.0},
                100.0,
            ),
            (
                ActionType.PRICE_CHANGE,
                {"new_price": 200.0},
                100.0,
            ),
            (
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": 110.0},
                100.0,
            ),
            (
                ActionType.MENU_SWAP,
                {"action": "remove"},
                None,
            ),
            (
                ActionType.STAFFING_CHANGE,
                {"headcount_delta": 1},
                None,
            ),
        ],
    )
    def test_reason_is_present_for_every_supported_policy_path(
        self,
        action_type,
        payload,
        current_value,
    ):
        result = classify_risk(
            action_type,
            payload,
            prediction(1.0),
            current_value,
        )

        assert result.reason
        assert result.reason.strip()
        assert len(result.reason.strip()) >= 10

    def test_high_confidence_reason_mentions_confidence_when_blocked(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.50),
            100.0,
        )

        assert_high(result)
        assert "confidence" in result.reason.lower()

    def test_invalid_numeric_reason_is_actionable(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": "105"},
            prediction(0.99),
            100.0,
        )

        assert_high(result)

        reason = result.reason.lower()

        assert (
            "finite" in reason
            or "number" in reason
            or "numeric" in reason
            or "invalid" in reason
        )


# ---------------------------------------------------------------------------
# Serialization / representation safety
# ---------------------------------------------------------------------------


class TestResultRepresentation:
    def test_result_has_no_extra_mutable_state(self):
        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"new_price": 105.0},
            prediction(0.99),
            100.0,
        )

        assert result.__dataclass_params__.frozen is True

    def test_result_equality_is_value_based(self):
        first = RiskClassification(
            RiskLevel.LOW,
            True,
            False,
            "same",
        )

        second = RiskClassification(
            RiskLevel.LOW,
            True,
            False,
            "same",
        )

        assert first == second

    def test_result_can_be_used_as_dict_key(self):
        result = RiskClassification(
            RiskLevel.LOW,
            True,
            False,
            "same",
        )

        mapping = {result: "accepted"}

        assert mapping[result] == "accepted"