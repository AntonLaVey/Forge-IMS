"""Authentication endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    create_session,
    get_bearer_token,
    get_client_ip,
    get_current_user,
    login_attempts,
    revoke_session,
)
from app.core.config import settings

router = APIRouter(prefix="/auth")


class LoginPayload(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: str) -> str:
        if not value.isdigit() or len(value) not in {4, 6}:
            raise ValueError("PIN must be 4 or 6 digits")
        return value


async def _failure_delay() -> None:
    delay_seconds = max(settings.LOGIN_FAILURE_DELAY_MS, 0) / 1000
    if delay_seconds:
        await asyncio.sleep(delay_seconds)


@router.post("/login")
async def login(payload: LoginPayload, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    retry_after = login_attempts.get_retry_after(client_ip)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    result = await db.execute(
        text(
            """
            SELECT id, name, role FROM users
            WHERE pin_hash = crypt(:pin, pin_hash) AND is_active = TRUE
            LIMIT 1
            """
        ),
        {"pin": payload.pin},
    )
    user = result.fetchone()
    if not user:
        login_attempts.record_failure(client_ip)
        await _failure_delay()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid PIN")

    login_attempts.clear(client_ip)
    token = create_session(str(user.id), user.role, user.name)
    await db.execute(
        text("UPDATE users SET last_login = NOW() WHERE id = :id"),
        {"id": str(user.id)},
    )
    return {
        "token": token,
        "user": {"id": str(user.id), "name": user.name, "role": user.role},
    }


@router.post("/logout")
async def logout(token: str = Depends(get_bearer_token), user=Depends(get_current_user)):
    revoke_session(token)
    return {"message": f"Logged out {user['name']}"}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return user
