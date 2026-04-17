"""Admin authentication: signed-cookie sessions + Google SSO domain enforcement.

The admin session cookie carries a JSON payload `{"email", "iat", "exp"}` signed
with `itsdangerous.URLSafeTimedSerializer`. Validation is two-step:

1. Verify the signature and TTL.
2. Re-check the email domain matches `ADMIN_ALLOWED_DOMAIN` — even if a stale
   cookie survives a config change, an email outside the allowlist is rejected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog
from fastapi import Cookie, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

logger = structlog.get_logger(__name__)

ADMIN_COOKIE_NAME = "admin_session"
_SERIALIZER_SALT = "tedi-admin-session-v1"


@dataclass
class AdminPrincipal:
    email: str
    issued_at: int


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key=settings.ADMIN_SESSION_SECRET,
        salt=_SERIALIZER_SALT,
    )


def email_is_allowed(email: str | None) -> bool:
    """Return True iff `email` is verified Bone Echo staff."""
    if not email:
        return False
    email = email.strip().lower()
    domain = settings.ADMIN_ALLOWED_DOMAIN.strip().lower().lstrip("@")
    if not domain:
        return False
    return email.endswith("@" + domain)


def issue_session_cookie(response: Response, email: str) -> str:
    """Sign and attach the admin session cookie. Returns the raw token."""
    payload = {"email": email.strip().lower(), "iat": int(time.time())}
    token = _serializer().dumps(payload)
    secure = settings.APP_ENV == "production"
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        max_age=settings.ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return token


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")


def decode_session_cookie(token: str | None) -> AdminPrincipal | None:
    """Verify a signed admin cookie and return the principal or None."""
    if not token:
        return None
    try:
        payload = _serializer().loads(
            token, max_age=settings.ADMIN_SESSION_TTL_SECONDS
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    if not email_is_allowed(email):
        return None
    iat = payload.get("iat")
    if not isinstance(iat, int):
        return None
    return AdminPrincipal(email=email, issued_at=iat)


def require_admin(
    request: Request,
    admin_session: str | None = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> AdminPrincipal:
    """FastAPI dependency: 401 unless the request carries a valid admin cookie."""
    principal = decode_session_cookie(admin_session)
    if principal is None:
        logger.info(
            "admin_auth_rejected",
            path=request.url.path,
            client=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin authentication required",
        )
    return principal
