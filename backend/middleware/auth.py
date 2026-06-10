from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import settings


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Simple bearer token authentication middleware."""

    # Paths that don't require authentication
    PUBLIC_PATHS = {"/api/system/health", "/api/auth/login", "/api/github/webhook"}

    async def dispatch(self, request: Request, call_next):
        # Skip auth if no token is configured
        if not settings.auth_token:
            return await call_next(request)

        path = request.url.path

        # Skip auth for public paths, static files, and uploaded images (UUID filenames)
        if path in self.PUBLIC_PATHS or not path.startswith("/api") or path.startswith("/api/uploads/"):
            return await call_next(request)

        # Skip auth for WebSocket (handled separately)
        if path == "/ws":
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {settings.auth_token}":
            return await call_next(request)

        # Check query parameter (for convenience on mobile)
        token_param = request.query_params.get("token", "")
        if token_param == settings.auth_token:
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
