from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import settings


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token + JWT authentication middleware."""

    PUBLIC_PATHS = {
        "/api/system/health", "/api/auth/login", "/api/auth/register", "/api/auth/send-code",
        "/api/github/webhook",
        "/api/feishu/callback",
        "/api/shared/receive", "/api/shared/revoke",
        "/api/org/register",
    }

    _admin_user_id: int | None = None
    _admin_resolved: bool = False

    async def dispatch(self, request: Request, call_next):
        if not settings.auth_token:
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
            if not self._admin_resolved:
                await self._resolve_admin_id()
            request.state.user_id = self._admin_user_id
            request.state.user_role = "super_admin"
            request.state.auth_type = "token"
            return await call_next(request)

        # JWT token
        from backend.api.auth import decode_jwt
        payload = decode_jwt(token)
        if payload:
            request.state.user_id = payload.get("user_id")
            request.state.user_role = payload.get("role", "member")
            request.state.auth_type = "jwt"
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    @classmethod
    async def _resolve_admin_id(cls):
        from backend.database import async_session
        from backend.models.user import User
        from sqlalchemy import select
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(User.id).where(User.role == "admin").order_by(User.id).limit(1)
                )
                cls._admin_user_id = result.scalar_one_or_none()
        except Exception:
            cls._admin_user_id = None
        cls._admin_resolved = True
