"""Email verification code service via SMTP."""

import logging
import random
import smtplib
import time
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.feishu.cn"
SMTP_PORT = 465
SMTP_USER = "admin@apexin.ai"
SMTP_PASS = "FbMp63itqVeirGJc"

# In-memory code store: {email: (code, expire_timestamp)}
_codes: dict[str, tuple[str, float]] = {}
CODE_EXPIRE_SECONDS = 300  # 5 minutes


def send_verification_code(email: str) -> bool:
    code = f"{random.randint(0, 999999):06d}"
    _codes[email] = (code, time.time() + CODE_EXPIRE_SECONDS)

    subject = "CCM 注册验证码"
    body = f"您的验证码是：{code}\n\n有效期 5 分钟。\n\n— Claude Code Manager"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = email

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [email], msg.as_string())
        logger.info("Verification code sent to %s", email)
        return True
    except Exception:
        logger.exception("Failed to send verification code to %s", email)
        return False


def verify_code(email: str, code: str) -> bool:
    entry = _codes.get(email)
    if not entry:
        return False
    stored_code, expire_at = entry
    if time.time() > expire_at:
        _codes.pop(email, None)
        return False
    if stored_code != code:
        return False
    _codes.pop(email, None)
    return True
