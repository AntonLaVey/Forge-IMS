"""app/api/users.py"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field
from typing import Optional
from app.core.database import get_db
from app.core.security import require_admin

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    pin: str = Field(..., min_length=4, max_length=6)
    role: str = Field(..., pattern="^(ADMIN|STAFF)$")


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    pin: Optional[str] = Field(None, min_length=4, max_length=6)
    role: Optional[str] = Field(None, pattern="^(ADMIN|STAFF)$")
    is_active: Optional[bool] = None


@router.get("/")
async def list_users(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    result = await db.execute(text("""
        SELECT id, name, role, is_active, created_at, last_login
        FROM users ORDER BY created_at ASC
    """))
    return {"users": [
        {
            "id": str(r.id),
            "name": r.name,
            "role": r.role,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_login": r.last_login.isoformat() if r.last_login else None,
        } for r in result.fetchall()
    ]}


@router.post("/")
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    if payload.role == "ADMIN" and len(payload.pin) != 6:
        raise HTTPException(400, "Admin PIN must be exactly 6 digits")
    if payload.role == "STAFF" and len(payload.pin) != 4:
        raise HTTPException(400, "Staff PIN must be exactly 4 digits")
    if not payload.pin.isdigit():
        raise HTTPException(400, "PIN must contain digits only")

    check = await db.execute(
        text("SELECT id FROM users WHERE pin_hash = crypt(:pin, pin_hash)"),
        {"pin": payload.pin}
    )
    if check.fetchone():
        raise HTTPException(409, "That PIN is already in use")

    result = await db.execute(
        text("""
            INSERT INTO users (name, pin_hash, role)
            VALUES (:name, crypt(:pin, gen_salt('bf')), :role)
            RETURNING id, name, role, is_active
        """),
        {"name": payload.name, "pin": payload.pin, "role": payload.role}
    )
    await db.commit()
    row = result.fetchone()
    return {"message": "User created", "user": {"id": str(row.id), "name": row.name, "role": row.role}}


@router.put("/{user_id}")
async def update_user(user_id: str, payload: UserUpdate,
                      db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    updates, params = [], {"uid": user_id}
    if payload.name is not None:
        updates.append("name = :name"); params["name"] = payload.name
    if payload.pin is not None:
        if not payload.pin.isdigit():
            raise HTTPException(400, "PIN must contain digits only")
        check = await db.execute(
            text("SELECT id FROM users WHERE pin_hash = crypt(:pin, pin_hash) AND id != CAST(:uid AS UUID)"),
            {"pin": payload.pin, "uid": user_id}
        )
        if check.fetchone():
            raise HTTPException(409, "That PIN is already in use")
        updates.append("pin_hash = crypt(:pin, gen_salt('bf'))"); params["pin"] = payload.pin
    if payload.role is not None:
        updates.append("role = :role"); params["role"] = payload.role
    if payload.is_active is not None:
        updates.append("is_active = :is_active"); params["is_active"] = payload.is_active
    if not updates:
        raise HTTPException(400, "No fields to update")
    await db.execute(text(f"UPDATE users SET {', '.join(updates)} WHERE id = CAST(:uid AS UUID)"), params)
    await db.commit()
    return {"message": "User updated"}


@router.delete("/{user_id}")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    user_row = (
        await db.execute(
            text("SELECT id, name, is_active FROM users WHERE id = CAST(:uid AS UUID)"),
            {"uid": user_id},
        )
    ).fetchone()
    if not user_row:
        raise HTTPException(404, "User not found")

    if user_row.is_active:
        await db.execute(
            text("UPDATE users SET is_active = FALSE WHERE id = CAST(:uid AS UUID)"),
            {"uid": user_id},
        )
        await db.commit()
        return {"message": "User deactivated"}

    try:
        await db.execute(
            text("DELETE FROM users WHERE id = CAST(:uid AS UUID)"),
            {"uid": user_id},
        )
        await db.commit()
        return {"message": "User permanently deleted"}
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            "Cannot delete user because historical records reference this account.",
        )
