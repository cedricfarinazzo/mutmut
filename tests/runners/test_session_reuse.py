"""Tests for running mutants' tests from one collected pytest session (``reuse_test_session``)."""

import os
from pathlib import Path

import pytest

from mutmut.runners.harness import PytestRunner
from mutmut.workers.isolation import ForkServerRunner
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


def test_collection_errors_elsewhere_do_not_stop_a_worker_after_its_first_test(project):
    # A collection error counts as a failure: with -x it makes pytest set session.shouldfail
    # in the session server, which recent pytest versions refuse to unset in the worker.
    (project / "test_broken.py").write_text("this is not python\n")

    assert serve(
        [("pkg.x_f__mutmut_1", [A + "test_passes", A + "test_fails_when_mutant_active"])],
        runner_class=RecordingRunner,
    ) == [1]
    assert not Path("mutants/fallback.log").exists()


@pytest.mark.parametrize("reuse_session", [True, False])
def test_fork_server_workers_pass_under_filterwarnings_error(project, reuse_session):
    # Projects that turn warnings into errors must not see warnings caused by mutmut itself,
    # like a ResourceWarning for a replaced sys.stdout, in the middle of a test.
    (project.parent / "pytest.ini").write_text("[pytest]\nfilterwarnings =\n    error\n")

    def in_fork():
        runner = ForkServerRunner(
            max_workers=1, test_runner_class=PytestRunner, test_runner_args={}, reuse_session=reuse_session
        )
        runner.startup()
        try:
            runner.submit("pkg.x_f__mutmut_2", [A + "test_passes"], 60, 1.0)
            runner.submit("pkg.x_f__mutmut_1", [A + "test_passes", A + "test_fails_when_mutant_active"], 60, 1.0)
            runner.signal_work_complete()
            results = {}
            while runner.pending_count():
                result = runner.wait_for_result()
                results[result.mutant_name] = result.exit_code
            return results
        finally:
            runner.shutdown()

    assert run_in_fork_with_result(in_fork) == {"pkg.x_f__mutmut_2": 0, "pkg.x_f__mutmut_1": 1}
