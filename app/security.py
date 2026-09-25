from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(rounds))
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


def create_jwt(payload: dict[str, Any], secret: str, lifetime_seconds: int = 28_800) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = dict(payload, iat=int(time.time()), exp=int(time.time()) + lifetime_seconds)
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"


def decode_jwt(token: str, secret: str) -> dict[str, Any]:
    try:
        header, body, signature = token.split(".")
        expected = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        claims = json.loads(_unb64(body))
        if not hmac.compare_digest(signature, expected) or int(claims["exp"]) < time.time():
            raise ValueError("Invalid or expired token")
        return claims
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or expired token") from exc
