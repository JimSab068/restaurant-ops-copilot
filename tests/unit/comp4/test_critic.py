import logging
from decimal import Decimal

import pytest

from Component_1.models import ActionType
from Component_4.critic import (
    MAX_HEADCOUNT_DELTA,
    MAX_PRICE_CHANGE_PCT,
    MAX_SUPPLIER_PRICE_INCREASE_PCT,
    critique,
)


# ---------------------------------------------------------------------------
# PRICE_CHANGE
# ---------------------------------------------------------------------------

class TestPriceChange:
    def test_exactly_at_30_percent_is_approved(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 130.0},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_29_999_percent_is_approved(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 129.999},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_30_001_percent_is_blocked(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 130.001},
            current_value=100.0,
        )

        assert verdict.approved is False
        assert "exceeds" in verdict.reason

    def test_exactly_negative_30_percent_is_approved(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 70.0},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_negative_30_001_percent_is_blocked(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 69.999},
            current_value=100.0,
        )

        assert verdict.approved is False

    def test_missing_new_price_is_blocked(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {},
            current_value=100.0,
        )

        assert verdict.approved is False
        assert "new_price" in verdict.reason

    def test_none_new_price_is_blocked(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": None},
            current_value=100.0,
        )

        assert verdict.approved is False

    @pytest.mark.parametrize(
        "new_price",
        [
            "130.00",
            "not-a-number",
            object(),
            True,
            False,
        ],
    )
    def test_non_numeric_new_price_is_blocked(self, new_price):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            current_value=100.0,
        )

        assert verdict.approved is False

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
    def test_non_finite_new_price_is_blocked(self, new_price):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": new_price},
            current_value=100.0,
        )

        assert verdict.approved is False

    def test_decimal_values_are_supported(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": Decimal("129.99")},
            current_value=Decimal("100.00"),
        )

        assert verdict.approved is True


# ---------------------------------------------------------------------------
# PRICE_CHANGE current_value validation
# ---------------------------------------------------------------------------

class TestPriceChangeCurrentValue:
    @pytest.mark.parametrize(
        "current_value",
        [
            None,
            0,
            0.0,
            Decimal("0"),
        ],
    )
    def test_missing_or_zero_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False
        assert "current value" in verdict.reason

    @pytest.mark.parametrize(
        "current_value",
        [
            -1,
            -100.0,
            Decimal("-0.01"),
        ],
    )
    def test_negative_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False
        assert "greater than zero" in verdict.reason

    @pytest.mark.parametrize(
        "current_value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_non_finite_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False

    @pytest.mark.parametrize(
        "current_value",
        [
            "100.00",
            object(),
            True,
            False,
        ],
    )
    def test_non_numeric_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            {"new_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False


# ---------------------------------------------------------------------------
# SUPPLIER_SWITCH
# ---------------------------------------------------------------------------

class TestSupplierSwitch:
    def test_exactly_50_percent_increase_is_approved(self):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 150.0},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_49_999_percent_increase_is_approved(self):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 149.999},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_50_001_percent_increase_is_blocked(self):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 150.001},
            current_value=100.0,
        )

        assert verdict.approved is False
        assert "exceeds" in verdict.reason

    def test_supplier_price_decrease_is_approved(self):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 10.0},
            current_value=100.0,
        )

        assert verdict.approved is True

    def test_missing_new_supplier_price_is_blocked(self):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {},
            current_value=100.0,
        )

        assert verdict.approved is False
        assert "new_supplier_price" in verdict.reason

    @pytest.mark.parametrize(
        "new_price",
        [
            None,
            "150.00",
            "not-a-number",
            object(),
            True,
            False,
        ],
    )
    def test_invalid_new_supplier_price_is_blocked(self, new_price):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": new_price},
            current_value=100.0,
        )

        assert verdict.approved is False

    @pytest.mark.parametrize(
        "new_price",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_non_finite_supplier_price_is_blocked(self, new_price):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": new_price},
            current_value=100.0,
        )

        assert verdict.approved is False


class TestSupplierSwitchCurrentValue:
    @pytest.mark.parametrize(
        "current_value",
        [
            None,
            0,
            0.0,
            -1,
            -100.0,
            Decimal("0"),
            Decimal("-1"),
        ],
    )
    def test_invalid_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False

    @pytest.mark.parametrize(
        "current_value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_non_finite_current_value_is_blocked(self, current_value):
        verdict = critique(
            ActionType.SUPPLIER_SWITCH,
            {"new_supplier_price": 100.0},
            current_value=current_value,
        )

        assert verdict.approved is False


# ---------------------------------------------------------------------------
# STAFFING_CHANGE
# ---------------------------------------------------------------------------

class TestStaffingChange:
    @pytest.mark.parametrize(
        "delta",
        [
            -MAX_HEADCOUNT_DELTA,
            MAX_HEADCOUNT_DELTA,
        ],
    )
    def test_exact_boundary_is_approved(self, delta):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
        )

        assert verdict.approved is True

    @pytest.mark.parametrize(
        "delta",
        [
            -(MAX_HEADCOUNT_DELTA + 1),
            MAX_HEADCOUNT_DELTA + 1,
        ],
    )
    def test_beyond_boundary_is_blocked(self, delta):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
        )

        assert verdict.approved is False
        assert "exceeds" in verdict.reason

    def test_missing_headcount_delta_is_blocked(self):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {},
        )

        assert verdict.approved is False
        assert "headcount_delta" in verdict.reason

    @pytest.mark.parametrize(
        "delta",
        [
            None,
            "5",
            "five",
            object(),
            True,
            False,
        ],
    )
    def test_non_numeric_headcount_delta_is_blocked(self, delta):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
        )

        assert verdict.approved is False

    @pytest.mark.parametrize(
        "delta",
        [
            5.5,
            -5.5,
            Decimal("5.5"),
            Decimal("-5.5"),
        ],
    )
    def test_fractional_headcount_delta_is_blocked(self, delta):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": delta},
        )

        assert verdict.approved is False

    def test_integer_valued_float_is_accepted(self):
        verdict = critique(
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": 5.0},
        )

        assert verdict.approved is True


# ---------------------------------------------------------------------------
# MENU_SWAP
# ---------------------------------------------------------------------------

class TestMenuSwap:
    def test_menu_swap_remains_unconditionally_approved(self):
        """
        This test intentionally locks in the current known limitation.

        If MENU_SWAP becomes bounded in the future, this test should fail,
        forcing the policy change to be made explicitly rather than silently.
        """
        verdict = critique(
            ActionType.MENU_SWAP,
            {
                "action": "add",
                "new_price": 1_000_000_000,
            },
            current_value=None,
        )

        assert verdict.approved is True
        assert "not currently bounded" in verdict.reason


# ---------------------------------------------------------------------------
# GENERAL INPUT VALIDATION
# ---------------------------------------------------------------------------

class TestGeneralValidation:
    def test_non_dict_payload_is_blocked(self):
        verdict = critique(
            ActionType.PRICE_CHANGE,
            None,
            current_value=100.0,
        )

        assert verdict.approved is False
        assert "dictionary" in verdict.reason

    def test_unknown_action_type_is_blocked(self):
        verdict = critique(
            "delete_database",
            {},
        )

        assert verdict.approved is False
        assert "Unrecognized action type" in verdict.reason


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

class TestLogging:
    def test_approved_verdict_is_logged(self, caplog):
        with caplog.at_level(logging.INFO):
            verdict = critique(
                ActionType.PRICE_CHANGE,
                {"new_price": 110.0},
                current_value=100.0,
            )

        assert verdict.approved is True
        assert "critic_verdict" in caplog.text
        assert "Within price-change bounds." in caplog.text

    def test_blocked_verdict_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            verdict = critique(
                ActionType.PRICE_CHANGE,
                {"new_price": 131.0},
                current_value=100.0,
            )

        assert verdict.approved is False
        assert "critic_verdict" in caplog.text
        assert "exceeds" in caplog.text
