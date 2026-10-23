"""Token issuance and validation with replay protection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from .exceptions import AuthError


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True)
class AuthContext:
    """Authenticated principal context extracted from token."""

    user_id: str
    session_id: str
    scopes: list[str]
    jti: str


class TokenValidator:
    """Issue and validate signed opaque tokens with replay detection."""

    def __init__(self, signing_key: str):
        self._signing_key = signing_key.encode("utf-8")
        self._used_jti: set[str] = set()

    def issue_token(
        self,
        user_id: str,
        session_id: str,
        scopes: list[str],
        ttl_seconds: int = 300,
    ) -> str:
        now = int(time.time())
        payload = {
            "sub": user_id,
            "sid": session_id,
            "scp": scopes,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": secrets.token_hex(12),
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self._signing_key, payload_bytes, hashlib.sha256).digest()
        return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"

    def validate(self, token: str) -> AuthContext:
        try:
            payload_b64, sig_b64 = token.split(".", 1)
        except ValueError as exc:
            raise AuthError("TOKEN_INVALID", "Malformed token") from exc

        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
        expected_sig = hmac.new(self._signing_key, payload_bytes, hashlib.sha256).digest()

        if not hmac.compare_digest(provided_sig, expected_sig):
            raise AuthError("TOKEN_INVALID", "Token signature mismatch")

        payload = json.loads(payload_bytes.decode("utf-8"))

        if int(payload.get("exp", 0)) < int(time.time()):
            raise AuthError("TOKEN_EXPIRED", "Token expired")

        jti = str(payload.get("jti", ""))
        if not jti:
            raise AuthError("TOKEN_INVALID", "Missing token identifier")

        if jti in self._used_jti:
            raise AuthError("TOKEN_REPLAY", "Token replay detected")

        self._used_jti.add(jti)

        scopes = payload.get("scp")
        if not isinstance(scopes, list):
            raise AuthError("TOKEN_INVALID", "Invalid scopes field")

        return AuthContext(
            user_id=str(payload.get("sub", "")),
            session_id=str(payload.get("sid", "")),
            scopes=[str(scope) for scope in scopes],
            jti=jti,
        )
