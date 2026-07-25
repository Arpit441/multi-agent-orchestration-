"""Password session + API key authentication."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from agent_orchestrator.api.settings import Settings, get_settings

COOKIE_NAME = "orchestrator_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    secret = settings.session_secret or "dev-insecure-change-me"
    return URLSafeTimedSerializer(secret, salt="orchestrator-auth")


def auth_enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.app_password.strip())


def create_session_token(settings: Settings) -> str:
    return _serializer(settings).dumps({"role": "demo"})


def verify_session_token(token: str, settings: Settings) -> bool:
    try:
        _serializer(settings).loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def password_ok(password: str, settings: Settings) -> bool:
    expected = settings.app_password
    if not expected:
        return True
    return secrets.compare_digest(password, expected)


def api_key_ok(api_key: str | None, settings: Settings) -> bool:
    if not settings.api_key:
        return False
    if not api_key:
        return False
    return secrets.compare_digest(api_key, settings.api_key)


def is_authenticated(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not auth_enabled(settings):
        return True

    header_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if api_key_ok(header_key, settings):
        return True

    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if api_key_ok(token, settings) or verify_session_token(token, settings):
            return True

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and verify_session_token(cookie, settings):
        return True
    return False


def set_session_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_token(settings),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def require_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if is_authenticated(request, settings):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Log in or provide X-API-Key.",
        headers={"WWW-Authenticate": "Bearer"},
    )
