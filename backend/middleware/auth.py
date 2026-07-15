from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import settings


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token + JWT authentication middleware with role enforcement."""

    PUBLIC_PATHS = {
        "/api/system/health", "/api/auth/login", "/api/auth/register", "/api/auth/send-code",
        "/api/github/webhook",
        "/api/feishu/callback",
        "/api/shared/receive", "/api/shared/revoke",
        "/api/org/register",
    }

    # System-level paths: non-GET requires admin/super_admin
    ADMIN_ONLY_PREFIXES = (
        "/api/instances",
        "/api/dispatcher",
        "/api/pool",
        "/api/settings",
    )

    _admin_user_id: int | None = None
    _admin_resolved: bool = False

    async def dispatch(self, request: Request, call_next):
        if not settings.auth_token:
            # 无鉴权模式（AUTH_TOKEN 为空）：历史语义是完全开放。RBAC 守卫
            # （require_task_access / require_admin）需要请求身份，若不设置则
            # user_id=None + role=member → 全线 403，无鉴权部署整个不可用。
            # 故此模式下所有请求视为 super_admin（与「无鉴权 = 全开放」一致）。
            request.state.user_id = None
            request.state.user_role = "super_admin"
            request.state.auth_type = "none"
            return await call_next(request)

        path = request.url.path

        if path in self.PUBLIC_PATHS or not path.startswith("/api") or path.startswith("/api/uploads/"):
            return await call_next(request)

        if path.startswith("/api/shared-access/"):
            return await call_next(request)

        if path in ("/ws", "/ws/shared"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""

        if not token:
            token = request.query_params.get("token", "")

        if not token:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Legacy token → resolve to default admin account
        if token == settings.auth_token:
            if not self._admin_resolved or self._admin_user_id is None:
                await self._resolve_admin_id()
            request.state.user_id = self._admin_user_id
            request.state.user_role = "super_admin"
            request.state.auth_type = "token"
        else:
            # JWT token
            from backend.api.auth import decode_jwt
            payload = decode_jwt(token)
            if payload:
                request.state.user_id = payload.get("user_id")
                request.state.user_role = payload.get("role", "member")
                request.state.auth_type = "jwt"
            else:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Enforce admin-only on system-level write operations
        if request.method != "GET":
            role = getattr(request.state, "user_role", "member")
            if role not in ("admin", "super_admin"):
                for prefix in self.ADMIN_ONLY_PREFIXES:
                    if path.startswith(prefix):
                        return JSONResponse(status_code=403, content={"detail": "Admin only"})

        return await call_next(request)

    @classmethod
    async def _resolve_admin_id(cls):
        from backend.database import async_session
        from backend.models.user import User
        from sqlalchemy import select
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(User.id).where(User.role.in_(["admin", "super_admin"])).order_by(User.id).limit(1)
                )
                cls._admin_user_id = result.scalar_one_or_none()
        except Exception:
            cls._admin_user_id = None
        cls._admin_resolved = True
