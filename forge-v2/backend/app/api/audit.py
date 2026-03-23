"""app/api/audit.py"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from app.core.database import get_db
from app.core.security import require_admin
from app.services.inventory import AuditService

router = APIRouter(prefix="/audit")

@router.get("")
async def list_audit(
    table_name: Optional[str] = None,
    record_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
):
    where = "WHERE 1=1"
    params: dict = {"limit": 50, "offset": (page-1)*50}
    if table_name:
        where += " AND a.table_name=:t"; params["t"] = table_name
    if record_id:
        where += " AND a.record_id=:r"; params["r"] = record_id
    result = await db.execute(text(f"""
        SELECT a.*, u.name AS performer_name FROM audit_snapshots a
        JOIN users u ON u.id = a.performed_by
        {where} ORDER BY a.performed_at DESC LIMIT :limit OFFSET :offset
    """), params)
    return [dict(r._mapping) for r in result.fetchall()]

@router.post("/{snapshot_id}/rollback")
async def rollback(snapshot_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    audit = AuditService(db, user["user_id"])
    success = await audit.rollback_field(snapshot_id)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(400, "Cannot rollback: already rolled back or not found")
    return {"message": "Rolled back"}
