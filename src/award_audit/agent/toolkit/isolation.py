"""Terminateable process isolation for high-risk local evidence parsers."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from multiprocessing.connection import wait
from typing import Any, TypeVar, cast

T = TypeVar("T")


class IsolatedCallError(RuntimeError):
    """An isolated function raised an exception without exposing a traceback."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class IsolatedCallTimeout(IsolatedCallError):
    """An isolated worker was terminated at its hard deadline."""

    def __init__(self) -> None:
        super().__init__("TimeoutError", "isolated parser exceeded its hard timeout")


def _worker(
    sender: Any,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    try:
        result = function(*args, **kwargs)
        sender.send(("ok", result))
    except BaseException as exc:
        sender.send(("error", type(exc).__name__, str(exc)[:1000]))
    finally:
        sender.close()


def run_isolated(
    function: Callable[..., T],
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> T:
    """Run one importable callable in a spawn process and kill it on timeout."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(sender, function, args, kwargs or {}),
        name="award-audit-isolated-tool",
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        ready = wait([receiver, process.sentinel], timeout_seconds)
        if receiver not in ready:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(2)
            raise IsolatedCallTimeout
        payload = receiver.recv()
        process.join(2)
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(2)
    if payload[0] == "ok":
        return cast(T, payload[1])
    raise IsolatedCallError(str(payload[1]), str(payload[2]))
