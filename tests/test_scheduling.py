"""Tests for the order in which mutmut runs mutants and their tests."""

import pytest

from mutmut.__main__ import order_tests_fastest_first
from mutmut.state import reset_state
from mutmut.state import state


@pytest.fixture(autouse=True)
def fresh_state():
    reset_state()
    yield
    reset_state()


def test_order_tests_fastest_first_orders_by_recorded_duration():
    state().duration_by_test.update({"t_slow": 3.0, "t_fast": 0.1, "t_mid": 1.0})

    assert order_tests_fastest_first({"t_slow", "t_mid", "t_fast"}) == ["t_fast", "t_mid", "t_slow"]


def test_order_tests_fastest_first_treats_unknown_tests_as_instant_and_records_nothing():
    state().duration_by_test["t_known"] = 1.0

    assert order_tests_fastest_first(["t_known", "t_unknown"]) == ["t_unknown", "t_known"]
    assert "t_unknown" not in state().duration_by_test
