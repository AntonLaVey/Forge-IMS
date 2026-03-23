"""Cycle count and category endpoints."""
from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin

cycle_count_router = APIRouter(prefix="/cycle-count")


@cycle_count_router.post("/generate")
async def generate_count(db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    result = await db.execute(text("SELECT generate_cycle_count() AS session_id"))
    sid = result.scalar()
    return {"session_id": str(sid)}


@cycle_count_router.get("/today")
async def today_count(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text(
            """
            SELECT cci.*, a.name AS asset_name, a.sku, l.code AS location
            FROM cycle_count_sessions ccs
            JOIN cycle_count_items cci ON cci.session_id = ccs.id
            JOIN assets a ON a.id = cci.asset_id
            LEFT JOIN locations l ON l.id = a.location_id
            WHERE ccs.session_date = CURRENT_DATE
            ORDER BY a.name
            """
        )
    )
    return [dict(r._mapping) for r in result.fetchall()]


@cycle_count_router.patch("/items/{item_id}")
async def submit_count(
    item_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await db.execute(
        text(
            """
            UPDATE cycle_count_items
            SET counted_qty=:qty, is_verified=TRUE,
                counted_by=:uid, counted_at=NOW(), notes=:notes
            WHERE id=:id
            """
        ),
        {
            "qty": payload["counted_qty"],
            "uid": user["user_id"],
            "notes": payload.get("notes"),
            "id": item_id,
        },
    )
    return {"message": "Count recorded"}


router = APIRouter(prefix="/categories")


@router.get("")
async def list_categories(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("SELECT * FROM categories ORDER BY name"))
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("", status_code=201)
async def create_category(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    cid = str(_uuid.uuid4())
    await db.execute(
        text("INSERT INTO categories (id, name, description) VALUES (:id, :name, :desc)"),
        {"id": cid, "name": name, "desc": payload.get("description")},
    )
    await db.commit()
    return {"id": cid, "name": name, "message": "Category created"}
