"""app/api/locations.py — Location CRUD (was missing entirely)"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
import uuid as _uuid

router = APIRouter(prefix="/locations")


@router.get("")
async def list_locations(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT l.*, COUNT(a.id) AS asset_count
        FROM locations l
        LEFT JOIN assets a ON a.location_id = l.id
        WHERE l.is_active = TRUE
        GROUP BY l.id
        ORDER BY l.code
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/{location_id}")
async def get_location(location_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text("SELECT * FROM locations WHERE id = :id"),
        {"id": location_id}
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Location not found")
    return dict(row._mapping)


@router.post("", status_code=201)
async def create_location(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    code = (payload.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "Location code is required")
    lid = str(_uuid.uuid4())
    try:
        await db.execute(
            text("INSERT INTO locations (id, code, description) VALUES (:id, :code, :desc)"),
            {"id": lid, "code": code, "desc": payload.get("description")}
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "unique" in str(e).lower():
            raise HTTPException(409, f"Location code '{code}' already exists")
        raise HTTPException(500, str(e))
    return {"id": lid, "code": code, "message": "Location created"}


@router.patch("/{location_id}")
async def update_location(location_id: str, payload: dict,
                          db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    fields = []
    params = {"id": location_id}
    if "code" in payload:
        fields.append("code = :code")
        params["code"] = payload["code"]
    if "description" in payload:
        fields.append("description = :description")
        params["description"] = payload["description"]
    if not fields:
        raise HTTPException(400, "No fields to update")
    await db.execute(
        text(f"UPDATE locations SET {', '.join(fields)} WHERE id = :id"),
        params
    )
    await db.commit()
    return {"message": "Location updated"}


@router.delete("/{location_id}")
async def delete_location(location_id: str,
                           db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    # Check if any assets are assigned here
    check = await db.execute(
        text("SELECT COUNT(*) FROM assets WHERE location_id = :id"),
        {"id": location_id}
    )
    count = check.scalar()
    if count > 0:
        raise HTTPException(409, f"Cannot delete: {count} asset(s) assigned to this location. Reassign them first.")
    await db.execute(
        text("UPDATE locations SET is_active = FALSE WHERE id = :id"),
        {"id": location_id}
    )
    await db.commit()
    return {"message": "Location deleted"}
