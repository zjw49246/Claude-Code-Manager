"""Authentication API: JWT login, registration, and legacy token support."""

import logging
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_SECRET = settings.auth_token or "ccm-default-secret-change-me"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_jwt(user: User) -> str:
    payload = {
        "user_id": user.id,
        "email": user.email,
        "role": user.role,
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# --- Schemas ---

class LoginRequest(BaseModel):
    token: str = ""
    email: str = ""
    password: str = ""


class SendCodeRequest(BaseModel):
    email: str


class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str
    code: str


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    role: str
    avatar_url: str
    feishu_open_id: str
    feishu_name: str


# --- Endpoints ---

@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Legacy token login (backward compatible)
    if body.token:
        if not settings.auth_token:
            return {"ok": True, "message": "No auth configured"}
        if body.token == settings.auth_token:
            return {"ok": True, "auth_type": "token"}
        raise HTTPException(401, "Invalid token")

    # JWT login with email + password
    if not body.email or not body.password:
        raise HTTPException(400, "Email and password required")

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account is disabled")

    user.last_login_at = datetime.utcnow()
    await db.commit()

    token = create_jwt(user)
    return {
        "ok": True,
        "auth_type": "jwt",
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url,
        },
    }


@router.post("/send-code")
async def send_code(body: SendCodeRequest):
    from backend.services.email_service import send_verification_code
    ok = send_verification_code(body.email)
    if not ok:
        raise HTTPException(500, "Failed to send verification code")
    return {"ok": True}


@router.post("/register")
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Verify email code
    from backend.services.email_service import verify_code
    if not verify_code(body.email, body.code):
        raise HTTPException(400, "Invalid or expired verification code")

    # Check duplicate email
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(409, "Email already registered")

    # First user becomes admin
    count_result = await db.execute(select(User))
    is_first = len(count_result.scalars().all()) == 0

    user = User(
        email=body.email,
        name=body.name,
        password_hash=_hash_password(body.password),
        role="admin" if is_first else "member",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_jwt(user)
    return {
        "ok": True,
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }


@router.get("/me")
async def get_me(request, db: AsyncSession = Depends(get_db)):
    from starlette.requests import Request
    request: Request = request
    auth_type = getattr(request.state, "auth_type", None)
    if auth_type == "token":
        return {"ok": True, "auth_type": "token", "role": "admin"}

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    return {
        "ok": True,
        "auth_type": "jwt",
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url,
            "feishu_open_id": user.feishu_open_id,
            "feishu_name": user.feishu_name,
        },
    }
