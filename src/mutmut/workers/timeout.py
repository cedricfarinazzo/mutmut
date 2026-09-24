import heapq
import os
import signal
import time
from collections import deque
from threading import Condition
from threading import Thread

_timeout_heap: list[tuple[float, int]] = []  # (timeout_timestamp, pid)
_heap_lock = Condition()
_checker_started = False


def register_timeout(pid: int, timeout_s: float) -> None:
    """Register a timeout for a given PID.

    Starts the timeout checker thread if not already started.

    On timeout sends SIGXCPU to the process.

    Args:
        pid: The process ID to register the timeout for.
        timeout_s: The number of seconds until the timeout occurs.
    """
    global _checker_started
    if not _checker_started:
        _checker_started = True
        Thread(target=_timeout_checker_thread, name=f"{os.getpid()}-mutmut-timeout-checker", daemon=True).start()

    deadline = time.time() + timeout_s
    with _heap_lock:
        heapq.heappush(_timeout_heap, (deadline, pid))
        _heap_lock.notify()


def _timeout_checker_thread() -> None:
    """Thread function that checks for timeouts and terminates processes.

    We make a trade-off here in the name of simplicity by not exposing a
    mechanism to cancel timeouts, which saves us an O(n) operation on each
    timeout we would cancel. Instead, we let expired entries for already-terminated
    processes remain in the heap until they reach the top and are popped off.
    The downside is a bit of memory bloat but each tuple is ~72 bytes so
    even with 10,000 backed up timeouts it's less than 1MB.
    """
    with _heap_lock:
        while True:
            while not _timeout_heap:
                _heap_lock.wait()
            now = time.time()
            while _timeout_heap and _timeout_heap[0][0] <= now:
                _, pid = heapq.heappop(_timeout_heap)
                try:
                    os.kill(pid, signal.SIGXCPU)
                except ProcessLookupError:
                    pass  # Process already terminated
            if _timeout_heap:
                _heap_lock.wait(timeout=max(0, _timeout_heap[0][0] - now))


class MutantTimeouts:
    """Wall-clock timeout for each mutant's test run.

    The default is ``(estimated test time + timeout_constant) * timeout_multiplier``. The
    estimate only counts the tests themselves, so the constant has to be generous enough to
    cover everything else a worker does (fixture setup, imports, ...), which for fast tests
    makes the timeout orders of magnitude longer than a normal run: every mutant that times
    out (an infinite loop, say) costs that much.

    With ``adaptive`` set, the time a worker takes on top of the estimate is measured on the
    mutants that finished normally. Once there are enough measurements, the timeout becomes
    ``(estimate + overhead) * timeout_multiplier``, using a high percentile of the measured
    overhead, never below ``timeout_constant`` and never above the default.
    """

    # Measurements needed before the adaptive timeout kicks in, and how many recent ones to keep.
    MIN_SAMPLES = 20
    WINDOW = 200
    PERCENTILE = 0.95

    def __init__(self, *, multiplier: float, constant: float, adaptive: bool) -> None:
        self.multiplier = multiplier
        self.constant = constant
        self.adaptive = adaptive
        self._overheads: deque[float] = deque(maxlen=self.WINDOW)

    def record(self, *, estimated_time: float, duration: float) -> None:
        """Record a mutant whose tests ran to completion (killed or survived, not timed out)."""
        if duration > 0:
            self._overheads.append(max(0.0, duration - estimated_time))

    def measured_overhead(self) -> float | None:
        if len(self._overheads) < self.MIN_SAMPLES:
            return None
        ordered = sorted(self._overheads)
        return ordered[min(len(ordered) - 1, int(len(ordered) * self.PERCENTILE))]

    def wall_timeout(self, estimated_time: float) -> float:
        default = (estimated_time + self.constant) * self.multiplier
        overhead = self.measured_overhead() if self.adaptive else None
        if overhead is None:
            return default
        return min(default, max(self.constant, (estimated_time + overhead) * self.multiplier))
