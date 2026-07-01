"""Shared FastAPI dependencies for user context."""

from fastapi import Request


def get_current_user_id(request: Request) -> int | None:
    return getattr(request.state, "user_id", None)


def get_current_user_role(request: Request) -> str:
    return getattr(request.state, "user_role", "member")


def is_admin(request: Request) -> bool:
    """Both admin and super_admin have admin-level permissions."""
    return get_current_user_role(request) in ("admin", "super_admin")


def is_super_admin(request: Request) -> bool:
    """Only super_admin can promote users to admin."""
    return get_current_user_role(request) == "super_admin"
