"""Tests for skipping the test listing of incremental runs when the test suite is unchanged."""

import json
from pathlib import Path

import pytest

from mutmut.__main__ import collect_or_load_stats
from mutmut.runners.harness import CollectTestsFailedException
from mutmut.runners.harness import ListAllTestsResult
from mutmut.state import reset_state
from mutmut.state import state
from mutmut.stats import save_stats

TEST_ID = "tests/test_a.py::test_a"
STATS_FILE = Path("mutants/mutmut-stats.json")


class FakeRunner:
    def __init__(self, fail=False):
        self.listings = 0
        self.fail = fail

    def list_all_tests(self):
        self.listings += 1
        if self.fail:
            raise CollectTestsFailedException()
        return ListAllTestsResult(ids={TEST_ID})


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "mutants" / "tests").mkdir(parents=True)
    (tmp_path / "mutants" / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    reset_state()
    state().duration_by_test[TEST_ID] = 0.1
    state().tests_by_mangled_function_name["pkg.x_f"].add(TEST_ID)
    state().stats_time = 1.0
    save_stats()
    reset_state()
    yield tmp_path
    reset_state()


def incremental_run(runner):
    reset_state()
    collect_or_load_stats(runner)
    return runner.listings


def test_unchanged_test_suite_skips_the_listing():
    assert incremental_run(FakeRunner()) == 1, "no fingerprint recorded yet, so the tests are listed"
    assert json.loads(STATS_FILE.read_text())["test_suite_fingerprint"]

    assert incremental_run(FakeRunner()) == 0


def test_changed_test_file_lists_the_tests_again(project):
    incremental_run(FakeRunner())
    (project / "mutants" / "tests" / "test_a.py").write_text("def test_a(): pass\ndef test_b(): pass\n")

    assert incremental_run(FakeRunner()) == 1
    assert incremental_run(FakeRunner()) == 0


def test_changed_pytest_config_outside_mutants_lists_the_tests_again(project):
    incremental_run(FakeRunner())
    (project / "pytest.ini").write_text("[pytest]\n")

    assert incremental_run(FakeRunner()) == 1


def test_bookkeeping_and_hidden_files_do_not_count(project):
    incremental_run(FakeRunner())
    (project / "mutants" / "tests" / "test_a.py.meta").write_text("{}")
    (project / "mutants" / ".pytest_cache").mkdir()
    (project / "mutants" / ".pytest_cache" / "lastfailed").write_text("{}")
    (project / "mutants" / "tests" / "__pycache__").mkdir()
    (project / "mutants" / "tests" / "__pycache__" / "test_a.cpython-310.pyc").write_bytes(b"x")

    assert incremental_run(FakeRunner()) == 0


def test_failed_listing_is_not_recorded(project):
    with pytest.raises(SystemExit):
        incremental_run(FakeRunner(fail=True))
    assert json.loads(STATS_FILE.read_text())["test_suite_fingerprint"] is None

    assert incremental_run(FakeRunner()) == 1
