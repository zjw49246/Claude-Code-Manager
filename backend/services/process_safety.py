"""Shared fail-closed validation for POSIX process-group operations."""


class UnsafeProcessGroupError(RuntimeError):
    """A PID/PGID could turn a targeted signal into a broad broadcast."""


def require_safe_process_group_id(
    process_group_id: object,
    *,
    context: str,
) -> int:
    """Return one exact signal-safe PGID or raise without signalling.

    POSIX implements ``killpg(pgid, sig)`` as ``kill(-pgid, sig)``.  PGID 1
    therefore becomes PID -1, whose special meaning is to signal every process
    the caller may target.  Boolean values are rejected as well because
    ``bool`` subclasses ``int`` in Python.
    """

    if type(process_group_id) is not int or process_group_id <= 1:
        raise UnsafeProcessGroupError(
            f"Refusing unsafe process group identity "
            f"{process_group_id!r} for {context}"
        )
    return process_group_id
