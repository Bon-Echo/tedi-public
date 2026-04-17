"""Google SSO endpoints for the admin dashboard.

Flow:
  GET /auth/google/login    — issue an OAuth `state`, redirect to Google
  GET /auth/google/callback — verify state, exchange code for tokens, validate
                              the ID token, enforce `@bonecho.ai`, set cookie
  POST /auth/logout         — clear the admin session cookie
  GET /auth/me              — return the current admin principal
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.middleware.admin_auth import (
    ADMIN_COOKIE_NAME,
    AdminPrincipal,
    clear_session_cookie,
    decode_session_cookie,
    email_is_allowed,
    issue_session_cookie,
    require_admin,
)
from app.services.admin_query import record_audit

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

OAUTH_STATE_COOKIE = "admin_oauth_state"
OAUTH_STATE_TTL_SECONDS = 600  # 10 min


def _ensure_oauth_configured() -> None:
    if not (
        settings.GOOGLE_OAUTH_CLIENT_ID
        and settings.GOOGLE_OAUTH_CLIENT_SECRET
        and settings.GOOGLE_OAUTH_REDIRECT_URI
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google SSO is not configured on this server.",
        )


@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
    """Redirect the browser to Google's OAuth consent screen."""
    _ensure_oauth_configured()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": settings.ADMIN_ALLOWED_DOMAIN,  # hosted-domain hint
        "access_type": "online",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    response = RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.APP_ENV == "production",
        samesite="lax",
        path="/auth",
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    admin_oauth_state: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Exchange the auth code, validate the ID token, set the admin cookie."""
    _ensure_oauth_configured()

    if error:
        logger.warning("google_oauth_provider_error", error=error)
        raise HTTPException(status_code=400, detail=f"google oauth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    if not admin_oauth_state or not secrets.compare_digest(admin_oauth_state, state):
        raise HTTPException(status_code=400, detail="invalid oauth state")

    token_payload = {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data=token_payload)
    if token_resp.status_code != 200:
        logger.warning(
            "google_oauth_token_exchange_failed",
            status=token_resp.status_code,
            body=token_resp.text[:300],
        )
        raise HTTPException(status_code=400, detail="token exchange failed")

    token_json = token_resp.json()
    id_token_jwt = token_json.get("id_token")
    if not id_token_jwt:
        raise HTTPException(status_code=400, detail="no id_token returned")

    # Verify ID token signature, audience, and issuer with google-auth.
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        id_info = google_id_token.verify_oauth2_token(
            id_token_jwt,
            google_requests.Request(),
            audience=settings.GOOGLE_OAUTH_CLIENT_ID,
        )
    except Exception as exc:
        logger.warning("google_id_token_invalid", error=str(exc))
        raise HTTPException(status_code=401, detail="invalid id token") from exc

    email = (id_info.get("email") or "").lower().strip()
    email_verified = bool(id_info.get("email_verified"))
    hosted_domain = id_info.get("hd")

    if not email_verified:
        logger.info("google_email_unverified", email=email)
        raise HTTPException(status_code=403, detail="email not verified by Google")

    if not email_is_allowed(email):
        logger.info(
            "google_email_domain_rejected",
            email=email,
            hosted_domain=hosted_domain,
            allowed_domain=settings.ADMIN_ALLOWED_DOMAIN,
        )
        raise HTTPException(
            status_code=403,
            detail=f"only {settings.ADMIN_ALLOWED_DOMAIN} accounts may sign in",
        )

    # Issue cookie and audit the login. Redirect back to the admin UI.
    redirect_target = settings.ADMIN_UI_ORIGIN.split(",")[0].strip().rstrip("/") + "/"
    response = RedirectResponse(
        url=redirect_target, status_code=status.HTTP_303_SEE_OTHER
    )
    issue_session_cookie(response, email)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")

    try:
        await record_audit(
            db,
            actor_email=email,
            action="admin.login",
            metadata={"ip": request.client.host if request.client else None},
        )
        await db.commit()
    except Exception:
        logger.exception("admin_login_audit_failed", actor=email)

    logger.info("admin_login_success", actor=email)
    return response


@router.post("/logout")
async def logout(response: Response) -> JSONResponse:
    out = JSONResponse({"ok": True})
    clear_session_cookie(out)
    return out


@router.get("/me")
async def me(
    admin_session: str | None = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> JSONResponse:
    principal = decode_session_cookie(admin_session)
    if principal is None:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse(
        {
            "authenticated": True,
            "email": principal.email,
            "issuedAt": principal.issued_at,
            "ttlSeconds": settings.ADMIN_SESSION_TTL_SECONDS,
            "now": int(time.time()),
        }
    )
