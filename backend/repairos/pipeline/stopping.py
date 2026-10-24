"""The one signal a worker that is shutting down raises from inside an inspection (task B10).

`WorkerStopping` means "release, not fail": the stop checks before every detector pass and vision-model call raise
it once the worker's stop flag is set (SIGTERM / SIGINT). Whoever catches it gives the work back: the still claim
goes back to pending (owner-checked), the job goes back to the queue with no attempt counted, and the still is
never marked failed. Any other exception is a real error and fails as before. Import it from here (vision code and
the pipeline share it without importing each other).
"""
from __future__ import annotations


class WorkerStopping(RuntimeError):
    """The worker is shutting down: the work in hand is released, never failed."""


def is_stopping(e: BaseException | None) -> bool:
    """Whether `e`, or an exception it was raised from, is WorkerStopping."""
    seen = set()
    while e is not None and id(e) not in seen:
        if isinstance(e, WorkerStopping):
            return True
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return False
