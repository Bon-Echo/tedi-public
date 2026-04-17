"""Tests for the admin auth helpers.

Covers acceptance criteria 3, 4, and the admin-cookie roundtrip.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.middleware.admin_auth import (
    ADMIN_COOKIE_NAME,
    decode_session_cookie,
    email_is_allowed,
    issue_session_cookie,
    require_admin,
)


# ---------------------------------------------------------------------------
# Domain check (Acceptance 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email, expected",
    [
        ("alice@bonecho.ai", True),
        ("ALICE@BoneCho.ai", True),  # case-insensitive
        ("bob@example.com", False),
        ("eve@evil.bonecho.ai.attacker.com", False),
        ("plain", False),
        ("", False),
        (None, False),
    ],
)
def test_email_is_allowed(email, expected):
    assert email_is_allowed(email) is expected


# ---------------------------------------------------------------------------
# Cookie roundtrip
# ---------------------------------------------------------------------------


def test_issue_and_decode_cookie_roundtrip():
    response = Response()
    token = issue_session_cookie(response, "alice@bonecho.ai")
    assert ADMIN_COOKIE_NAME in response.headers.get("set-cookie", "")

    principal = decode_session_cookie(token)
    assert principal is not None
    assert principal.email == "alice@bonecho.ai"
    # iat is present and fresh
    assert principal.issued_at <= int(time.time())
    assert principal.issued_at > int(time.time()) - 30


def test_decode_rejects_unsigned_token():
    assert decode_session_cookie("not-a-valid-signed-cookie") is None
    assert decode_session_cookie(None) is None
    assert decode_session_cookie("") is None


def test_decode_rejects_wrong_signature():
    """A token signed with a different secret must not validate."""
    other = URLSafeTimedSerializer(secret_key="different-secret", salt="tedi-admin-session-v1")
    token = other.dumps({"email": "alice@bonecho.ai", "iat": int(time.time())})
    assert decode_session_cookie(token) is None


def test_decode_rejects_expired_cookie(monkeypatch):
    """A cookie issued before TTL must be rejected."""
    monkeypatch.setattr(settings, "ADMIN_SESSION_TTL_SECONDS", 1)

    response = Response()
    token = issue_session_cookie(response, "alice@bonecho.ai")

    # itsdangerous compares integer-second timestamps with strict `>` against
    # max_age, so 2.2s comfortably exceeds a 1s TTL window.
    time.sleep(2.2)
    assert decode_session_cookie(token) is None


def test_decode_rejects_non_allowed_domain_even_with_valid_signature():
    """A signed cookie carrying a non-bonecho.ai email must still be rejected.

    This protects against the case where the allowlist tightens after the
    cookie was minted.
    """
    serializer = URLSafeTimedSerializer(
        secret_key=settings.ADMIN_SESSION_SECRET, salt="tedi-admin-session-v1"
    )
    token = serializer.dumps({"email": "eve@evil.com", "iat": int(time.time())})
    assert decode_session_cookie(token) is None


# ---------------------------------------------------------------------------
# require_admin dependency (Acceptance 3)
# ---------------------------------------------------------------------------


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/admin/signups",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    return Request(scope)


def test_require_admin_rejects_missing_cookie():
    with pytest.raises(HTTPException) as exc_info:
        require_admin(_make_request(), admin_session=None)
    assert exc_info.value.status_code == 401


def test_require_admin_accepts_valid_cookie():
    response = Response()
    token = issue_session_cookie(response, "alice@bonecho.ai")
    principal = require_admin(_make_request(), admin_session=token)
    assert principal.email == "alice@bonecho.ai"
