"""Tests for the order in which mutmut runs mutants and their tests."""

import pytest

from mutmut.__main__ import order_mutants_longest_first
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


def test_order_mutants_longest_first_uses_estimated_worst_case_time():
    state().duration_by_test.update({"t_short": 0.1, "t_long": 5.0, "t_mid": 1.0})
    state().tests_by_mangled_function_name.update(
        {
            "pkg.mod.x_short": {"t_short"},
            "pkg.mod.x_long": {"t_long", "t_short"},
            "pkg.mod.x_mid": {"t_mid"},
        }
    )
    data = object()
    mutants = [
        (data, "pkg.mod.x_short__mutmut_1", None),
        (data, "pkg.mod.x_untested__mutmut_1", None),
        (data, "pkg.mod.x_long__mutmut_1", None),
        (data, "pkg.mod.x_mid__mutmut_1", None),
    ]

    assert [name for _, name, _ in order_mutants_longest_first(mutants)] == [
        "pkg.mod.x_long__mutmut_1",
        "pkg.mod.x_mid__mutmut_1",
        "pkg.mod.x_short__mutmut_1",
        "pkg.mod.x_untested__mutmut_1",
    ]
