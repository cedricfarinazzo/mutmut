from __future__ import annotations

import warnings

from mutmut.configuration import config
from mutmut.configuration import reset_config
from mutmut.state import reset_state
from mutmut.state import state

_DEPRECATED_STATE_ATTRS = frozenset(
    {
        "stats_time",
        "duration_by_test",
        "tests_by_mangled_function_name",
        "_stats",
        "_covered_lines",
        "_excluded_lines",
    }
)


def __getattr__(name: str) -> object:
    match name:
        case "__version__":
            # Computed on first use: importing importlib.metadata takes ~30ms, and every process
            # that imports mutated code imports mutmut (via the trampoline), including the
            # subprocesses a test suite may start.
            import importlib.metadata

            version = importlib.metadata.version("mutmut")
            globals()["__version__"] = version
            return version
        case "config":
            warnings.warn(
                "mutmut.config is deprecated as of 3.4.1, use mutmut.configuration.config() instead",
                FutureWarning,
                stacklevel=2,
            )
            return config()
        case name if name in _DEPRECATED_STATE_ATTRS:
            warnings.warn(
                f"mutmut.{name} is deprecated, use mutmut.state.state().{name} instead",
                FutureWarning,
                stacklevel=2,
            )
            return getattr(state(), name)
        case _:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _reset_globals() -> None:
    reset_config()
    reset_state()
