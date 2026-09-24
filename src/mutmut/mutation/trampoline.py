import inspect
import os
from collections.abc import Callable
from functools import wraps
from typing import Annotated
from typing import Any
from typing import ParamSpec
from typing import TypeVar

from mutmut.configuration import config
from mutmut.core import MutmutCallStack
from mutmut.core import MutmutProgrammaticFailException
from mutmut.state import state
from mutmut.stats import record_trampoline_hit
from mutmut.utils.format_utils import mangled_name_from_mutant_name

TReturn = TypeVar("TReturn")
MutantDict = Annotated[dict[str, Callable[..., TReturn]], "Mutant"]

# mypy: disable-error-code="no-any-return, unused-ignore"


# properly typed decorator
P = ParamSpec("P")
R = TypeVar("R")

# mutant dict only contains some callable. maybe could be typed better, but likely not necessary.
F = TypeVar("F", bound=Callable[..., Any])


# How the trampoline should treat a call, derived from the active mutant name.
_MUTANT = 0  # "{module}.{mutant_name}", or "" / anything else: run the mutant if it is ours, else the original
_FAIL = 1  # "fail": raise, to verify that the tests notice
_STATS = 2  # "stats": run the original and record which tests reach it

# (kind, module, mutant name, full name). Parsed once per distinct value rather than on
# every call, because the trampoline sits on the hot path of every mutated function.
_ActiveMutant = tuple[int, str, str, str]


def _parse_mutant_under_test(name: str) -> _ActiveMutant:
    if name == "fail":
        return _FAIL, "", "", name
    if name == "stats":
        return _STATS, "", "", name
    module, _, mutant_name = name.rpartition(".")
    return _MUTANT, module, mutant_name, name


_NO_MUTANT: _ActiveMutant = _parse_mutant_under_test("")

# Process-local copy of the active mutant. Tests that scrub os.environ
# (``patch.dict(..., clear=True)``) must not be able to disable it. See #511.
_mutant_under_test: str | None = None
_local_active: _ActiveMutant = _NO_MUTANT

_ENV_KEY = "MUTANT_UNDER_TEST"

# Reading ``os.environ[...]`` encodes the key on every access, which made the environment
# lookup most of the trampoline's cost. On POSIX, ``os.environ`` is backed by a plain dict
# of encoded keys and values, so we read that directly: same data, a fraction of the cost.
_ENV_KEY_ENCODED = os.fsencode(_ENV_KEY)
_environ_is_bytes_backed = os.name == "posix" and isinstance(getattr(os.environ, "_data", None), dict)

# The last environment value we parsed and what it parsed to, as one tuple so that
# threads always see a matching pair.
_cached_environ: tuple[bytes | str | None, _ActiveMutant] = (None, _NO_MUTANT)


def set_mutant_under_test(name: str | None) -> None:
    """Record the active mutant in process-local state.

    ``None`` clears the override so the trampoline falls back to ``os.environ``.
    A string value is also mirrored into ``MUTANT_UNDER_TEST`` for compatibility.
    """
    global _mutant_under_test, _local_active
    _mutant_under_test = name
    _local_active = _NO_MUTANT if name is None else _parse_mutant_under_test(name)
    if name is not None:
        os.environ[_ENV_KEY] = name


def _active_mutant() -> _ActiveMutant:
    """The parsed active mutant, see ``get_mutant_under_test`` for the precedence rules."""
    global _cached_environ
    environ = os.environ
    if _environ_is_bytes_backed:
        raw: bytes | str | None = environ._data.get(_ENV_KEY_ENCODED)  # type: ignore[attr-defined]
    else:
        raw = environ.get(_ENV_KEY)
    if raw is None:
        return _local_active
    cached_raw, cached_active = _cached_environ
    if raw != cached_raw:
        cached_active = _parse_mutant_under_test(os.fsdecode(raw))
        _cached_environ = (raw, cached_active)
    return cached_active


def get_mutant_under_test() -> str:
    """Return the active mutant name.

    If ``MUTANT_UNDER_TEST`` is in the environment, that value wins so
    existing tests and callers that only set the env var keep working.
    If the key is missing (for example after ``patch.dict(..., clear=True)``),
    fall back to the process-local copy set by ``set_mutant_under_test``.
    """
    return _active_mutant()[3]


# Maximum call depth for dependency tracking during stats collection. ``None`` means
# "not set yet, read ``MUTMUT_DEPENDENCY_DEPTH``". Cached because the trampoline is on the
# hot path of every call to every mutated function while stats are collected.
_dependency_depth: int | None = None


def set_dependency_depth(depth: int | None) -> None:
    global _dependency_depth
    _dependency_depth = depth


def _get_dependency_depth() -> int:
    global _dependency_depth
    if _dependency_depth is None:
        _dependency_depth = int(os.environ.get("MUTMUT_DEPENDENCY_DEPTH", "-1"))
    return _dependency_depth


def _needs_recording(name: str, caller: str | None) -> bool:
    """Whether ``record_trampoline_hit`` could still learn something from this call.

    The hit set is per test (cleared at teardown) and the dependency edges are per function,
    so once both are known, the (expensive) stack inspection in ``record_trampoline_hit``
    cannot change the outcome and is skipped.
    """
    current = state()
    if name not in current._stats:
        return True
    if caller is None or not config().track_dependencies:
        return False
    callers = current.function_dependencies.get(name)
    return callers is None or caller not in callers


def wrap_in_trampoline(
    mutants_dict: dict[str, F], is_classmethod: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def mutmut_mutated(decorated_func: Callable[P, R]) -> Callable[P, R]:
        """Wrap the ``decorated_func`` in a trampoline.
        The trampoline forwards calls based on the active mutant
        (see ``get_mutant_under_test``), either to a copy of the original method,
        or to the currently active mutated method.
        """

        # qualified name of the original function, computed on the first stats hit
        cached_qual_name: str | None = None
        func_module = decorated_func.__module__

        def trampoline(*args: P.args, **kwargs: P.kwargs) -> R:
            nonlocal cached_qual_name
            kind, module, mutant_name, _ = _active_mutant()

            if kind == _MUTANT:
                # mutant under test is {module}.{mutant_name}; it only concerns us if the module matches
                if module == func_module:
                    mutated_func = mutants_dict.get(mutant_name)
                    if mutated_func is not None:
                        if is_classmethod:
                            return getattr(args[0], mutated_func.__name__)(*args[1:], **kwargs)
                        return mutated_func(*args, **kwargs)
                # no mutant of this function is active -> call the original function.
                # orig_func is the non-mutated implementation. We do not use `decorated_func`
                # directly, because using the func via SomeClass.foo makes it easier for
                # classmethod wrapping
                orig_func = mutants_dict["_mutmut_orig"]
                if is_classmethod:
                    # for @classmethod, the first arg is cls
                    # with getattr(cls, 'some_method'), we get cls.some_method
                    # which is necessary to get the method bound to the subclass, even if it's declared on the parent class
                    return getattr(args[0], orig_func.__name__)(*args[1:], **kwargs)
                return orig_func(*args, **kwargs)

            if kind == _FAIL:
                raise MutmutProgrammaticFailException(
                    "Verifying setup. At least one test should fail if mutations cause errors."
                )

            # kind == _STATS
            orig_func = mutants_dict["_mutmut_orig"]
            call_args: tuple[Any, ...] = args
            if is_classmethod:
                call_args = args[1:]
                orig_func = getattr(args[0], orig_func.__name__)

            if cached_qual_name is None:
                cached_qual_name = f"{orig_func.__module__}.{mangled_name_from_mutant_name(orig_func.__name__)}"
            orig_qual_name = cached_qual_name
            caller_name, depth = MutmutCallStack.get()
            max_depth = _get_dependency_depth()
            if max_depth == -1 or depth < max_depth:
                if _needs_recording(orig_qual_name, caller_name):
                    record_trampoline_hit(orig_qual_name, caller=caller_name)
                token = MutmutCallStack.set((orig_qual_name, depth + 1))
                try:
                    return orig_func(*call_args, **kwargs)
                finally:
                    MutmutCallStack.reset(token)
            else:
                return orig_func(*call_args, **kwargs)

        # ensure that inspect calls still produce the same result for the trampoline
        # @wraps sadly does not preserve this, so we do all cases manually here
        if inspect.isgeneratorfunction(decorated_func):

            @wraps(decorated_func)
            def _trampoline_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore
                # ``return`` the delegation result so a generator's StopIteration value
                # (``return`` inside the generator) is forwarded to the caller's
                # ``yield from``, matching the PEP 380 expansion.
                return (yield from trampoline(*args, **kwargs))  # type: ignore
        elif inspect.iscoroutinefunction(decorated_func):

            @wraps(decorated_func)
            async def _trampoline_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore
                return await trampoline(*args, **kwargs)  # type: ignore
        elif inspect.isasyncgenfunction(decorated_func):

            @wraps(decorated_func)
            async def _trampoline_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore
                # Forward the full async-generator protocol (asend/athrow/aclose) to the inner
                # generator. A bare ``async for`` only forwards iteration, so aclose()/athrow()
                # would hit this wrapper instead of the wrapped generator -- breaking deterministic
                # cleanup (``finally:`` / ``except GeneratorExit:`` running synchronously on close)
                # and exception injection. This mirrors the PEP 380 ``yield from`` expansion,
                # adapted for the async-generator protocol. See
                # https://github.com/boxed/mutmut/issues/525.
                gen = trampoline(*args, **kwargs)  # type: ignore
                try:
                    yielded = await gen.asend(None)  # type: ignore
                    while True:
                        try:
                            sent = yield yielded
                        except GeneratorExit:
                            # caller closed us -> close the inner generator and propagate
                            await gen.aclose()  # type: ignore
                            raise
                        except BaseException as exc:
                            # caller threw into us -> forward the exception into the inner generator
                            yielded = await gen.athrow(exc)  # type: ignore
                        else:
                            # normal resume (__anext__ / asend) -> forward the sent value
                            yielded = await gen.asend(sent)  # type: ignore
                except StopAsyncIteration:
                    return
                finally:
                    await gen.aclose()  # type: ignore
        else:
            # A plain function needs no protocol forwarding, so the trampoline itself is the
            # wrapper: one Python frame less on every call.
            return wraps(decorated_func)(trampoline)

        return _trampoline_wrapper  # type: ignore

    return mutmut_mutated
