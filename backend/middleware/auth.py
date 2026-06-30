from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import settings


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token + JWT authentication middleware."""

    PUBLIC_PATHS = {
        "/api/system/health", "/api/auth/login", "/api/auth/register",
        "/api/github/webhook",
        "/api/feishu/callback",
        "/api/shared/receive", "/api/shared/revoke",
        "/api/org/register",
    }

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

        # Legacy token → treat as admin
        if token == settings.auth_token:
            request.state.user_id = None
            request.state.user_role = "admin"
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
