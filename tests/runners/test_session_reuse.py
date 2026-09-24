"""Tests for running mutants' tests from one collected pytest session (``reuse_test_session``)."""

import os
from pathlib import Path

import pytest

from mutmut.runners.harness import PytestRunner
from mutmut.workers.isolation import run_in_fork_with_result

TEST_FILE = """
import os

def test_passes():
    assert True

def test_fails():
    assert False

def test_fails_when_mutant_active():
    assert os.environ.get("MUTANT_UNDER_TEST") != "pkg.x_f__mutmut_1"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    tests_dir = tmp_path / "mutants" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text(TEST_FILE)
    monkeypatch.chdir(tmp_path)
    return tests_dir


class RecordingRunner(PytestRunner):
    """Records the fallback runs instead of starting a nested pytest."""

    def run_tests(self, *, mutant_name, tests):
        Path("fallback.log").open("a").write(f"{mutant_name} {' '.join(tests)}\n")
        return 42


def serve(requests, runner_class=PytestRunner):
    """Run the server loop in a fork, running each (mutant, tests) request in a forked worker."""

    def in_fork():
        results = []

        def loop(run_mutant_tests):
            for mutant_name, tests in requests:
                pid = os.fork()
                if pid == 0:
                    os.environ["MUTANT_UNDER_TEST"] = mutant_name
                    os._exit(run_mutant_tests(mutant_name, tests))
                _, status = os.waitpid(pid, 0)
                results.append(os.waitstatus_to_exitcode(status))

        runner_class().serve_mutants(loop, reuse_session=True)
        return results

    return run_in_fork_with_result(in_fork)


A = "tests/test_a.py::"


def test_exit_codes_match_a_pytest_run(project):
    assert serve(
        [
            ("pkg.x_f__mutmut_2", [A + "test_passes"]),
            ("pkg.x_f__mutmut_2", [A + "test_passes", A + "test_fails"]),
            ("pkg.x_f__mutmut_2", [A + "test_fails_when_mutant_active"]),
            ("pkg.x_f__mutmut_1", [A + "test_passes", A + "test_fails_when_mutant_active"]),
        ],
        runner_class=RecordingRunner,
    ) == [0, 1, 0, 1]
    assert not Path("mutants/fallback.log").exists(), "the collected session should have run every test"


def test_a_failure_in_one_worker_does_not_leak_into_the_next(project):
    assert serve(
        [
            ("pkg.x_f__mutmut_2", [A + "test_fails"]),
            ("pkg.x_f__mutmut_2", [A + "test_passes"]),
        ],
        runner_class=RecordingRunner,
    ) == [1, 0]
    assert not Path("mutants/fallback.log").exists()


def test_uncollected_tests_fall_back_to_a_regular_run(project):
    assert serve(
        [("pkg.x_f__mutmut_2", [A + "test_passes", A + "test_does_not_exist"])], runner_class=RecordingRunner
    ) == [42]
    assert Path("mutants/fallback.log").read_text() == (f"pkg.x_f__mutmut_2 {A}test_passes {A}test_does_not_exist\n")


def test_collection_errors_elsewhere_do_not_change_the_verdict(project):
    (project / "test_broken.py").write_text("this is not python\n")

    assert serve([("pkg.x_f__mutmut_2", [A + "test_passes"])], runner_class=RecordingRunner) == [0]
    assert not Path("mutants/fallback.log").exists()
