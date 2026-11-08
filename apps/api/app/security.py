"""Authentication / authorisation.

Production-only:

* The browser sends the Supabase access token; we verify it and use ``sub`` as
  the identity. Both signing schemes Supabase ships are accepted:
  HS256 tokens against ``SUPABASE_JWT_SECRET`` (legacy symmetric projects) and
  ES256/RS256 tokens against the project's published JWKS. New Supabase
  projects issue asymmetric tokens by default, so accepting only HS256 would
  reject every real session.
* There is no local/dev identity and no ``X-Dev-User`` header. If neither a
  verifying key nor a JWKS endpoint is available the service refuses to
  authenticate rather than guessing.

Internal worker/cron endpoints use a separate shared secret.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from jwt import PyJWKClient

from .config import settings

_ASYMMETRIC_ALGS = frozenset({"ES256", "RS256"})


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return PyJWKClient(url, cache_keys=True)


def _decode_token(token: str) -> dict[str, Any]:
    algorithm = jwt.get_unverified_header(token).get("alg", "")
    if algorithm == "HS256":
        if not settings.supabase_jwt_secret:
            raise jwt.InvalidKeyError("SUPABASE_JWT_SECRET is unset")
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    if algorithm in _ASYMMETRIC_ALGS:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            options={"verify_aud": False},
        )
    raise jwt.InvalidAlgorithmError(f"Unsupported JWT algorithm: {algorithm or 'none'}")


@dataclass(slots=True)
class Identity:
    user_id: str
    email: str | None
    name: str | None
    mode: str  # "supabase" (browser JWT) or "mcp" (server-side MCP operator)


async def get_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    if not settings.supabase_jwt_secret and not settings.supabase_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Authentication is not configured (no JWT secret or JWKS endpoint)",
        )

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    try:
        claims = await run_in_threadpool(_decode_token, token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, please sign in again") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid access token") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token has no subject")
    meta = claims.get("user_metadata") or {}
    return Identity(
        user_id=user_id,
        email=claims.get("email"),
        name=meta.get("full_name") or meta.get("name"),
        mode="supabase",
    )


def require_internal(authorization: str | None = Header(default=None)) -> None:
    """Guard for cron/worker callbacks."""
    expected = settings.api_internal_token
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Internal token not configured")
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal token")


IdentityDep = Depends(get_identity)
InternalDep = Depends(require_internal)
