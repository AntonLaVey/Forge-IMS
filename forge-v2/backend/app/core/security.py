"""PIN-based auth and signed bearer token utilities."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class RevokedTokenStore:
    def __init__(self, max_entries: int = 10000):
        self.max_entries = max_entries
        self._tokens: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [digest for digest, expires_at in self._tokens.items() if expires_at <= now]
        for digest in expired:
            self._tokens.pop(digest, None)
        if len(self._tokens) > self.max_entries:
            overflow = len(self._tokens) - self.max_entries
            for digest, _ in sorted(self._tokens.items(), key=lambda item: item[1])[:overflow]:
                self._tokens.pop(digest, None)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def revoke(self, token: str, expires_at: float) -> None:
        with self._lock:
            self._cleanup_locked()
            self._tokens[self._digest(token)] = expires_at

    def is_revoked(self, token: str) -> bool:
        with self._lock:
            self._cleanup_locked()
            return self._digest(token) in self._tokens


class LoginAttemptTracker:
    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: Dict[str, Deque[float]] = {}
        self._locked_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _cleanup_locked(self) -> None:
        now = time.time()
        for key in list(self._attempts.keys()):
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)
        for key, locked_until in list(self._locked_until.items()):
            if locked_until <= now:
                self._locked_until.pop(key, None)

    def get_retry_after(self, key: str) -> int:
        with self._lock:
            self._cleanup_locked()
            remaining = int(max(0, self._locked_until.get(key, 0) - time.time()))
            return remaining

    def is_locked(self, key: str) -> bool:
        return self.get_retry_after(key) > 0

    def record_failure(self, key: str) -> int:
        with self._lock:
            self._cleanup_locked()
            attempts = self._attempts.setdefault(key, deque())
            now = time.time()
            attempts.append(now)
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                locked_until = now + self.lockout_seconds
                self._locked_until[key] = locked_until
                self._attempts.pop(key, None)
                return int(self.lockout_seconds)
            return 0

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._locked_until.pop(key, None)


revoked_tokens = RevokedTokenStore(max_entries=settings.TOKEN_REVOKE_CACHE_MAX)
login_attempts = LoginAttemptTracker(
    max_attempts=settings.LOGIN_MAX_ATTEMPTS,
    window_seconds=settings.LOGIN_WINDOW_SECONDS,
    lockout_seconds=settings.LOGIN_LOCKOUT_SECONDS,
)


# Keeps backward compatibility for any imports that still reference this helper.
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def _sign(data: bytes) -> str:
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), data, hashlib.sha256).digest()
    return _b64url_encode(signature)


def _encode_payload(payload: dict) -> str:
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(encoded_payload.encode("utf-8"))
    return f"{encoded_payload}.{signature}"


def _decode_payload(token: str) -> dict:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid") from exc

    expected_signature = _sign(encoded_payload.encode("utf-8"))
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")

    try:
        payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid") from exc

    exp = float(payload.get("exp", 0))
    if exp <= time.time():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")
    return payload


def create_session(user_id: str, role: str, name: str, ttl: Optional[int] = None) -> str:
    now = int(time.time())
    payload = {
        "user_id": user_id,
        "role": role,
        "name": name,
        "iat": now,
        "exp": now + int(ttl or settings.SESSION_TTL_SECONDS),
        "jti": secrets.token_urlsafe(12),
    }
    return _encode_payload(payload)


def validate_session(token: str) -> Optional[dict]:
    if revoked_tokens.is_revoked(token):
        return None
    try:
        payload = _decode_payload(token)
    except HTTPException:
        return None
    return {
        "user_id": payload["user_id"],
        "role": payload["role"],
        "name": payload["name"],
        "exp": payload["exp"],
        "iat": payload["iat"],
        "jti": payload["jti"],
    }


def revoke_session(token: str) -> None:
    try:
        payload = _decode_payload(token)
    except HTTPException:
        return
    revoked_tokens.revoke(token, float(payload["exp"]))


bearer = HTTPBearer(auto_error=False)


def get_bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> str:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return credentials.credentials


def get_current_user(token: str = Depends(get_bearer_token)) -> dict:
    session = validate_session(token)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")
    return session


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
