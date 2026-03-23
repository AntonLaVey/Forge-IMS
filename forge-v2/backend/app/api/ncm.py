"""app/api/ncm.py"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.inventory import AuditService
import uuid as _uuid

router = APIRouter(prefix="/ncm")

@router.get("")
async def list_ncm(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT n.*, a.name AS asset_name, a.sku, u.name AS flagged_by_name
        FROM ncm_logs n JOIN assets a ON a.id = n.asset_id JOIN users u ON u.id = n.flagged_by
        ORDER BY n.flagged_at DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]

@router.post("")
async def flag_ncm(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    nid = str(_uuid.uuid4())
    audit = AuditService(db, user["user_id"])
    cost_row = await db.execute(text("SELECT unit_cost FROM assets WHERE id=:id"), {"id": payload["asset_id"]})
    cost = cost_row.scalar() or 0
    await db.execute(text("""
        INSERT INTO ncm_logs (id,asset_id,qty_flagged,reason,description,unit_cost_at_flag,flagged_by)
        VALUES (:id,:aid,:qty,:reason,:desc,:cost,:uid)
    """), {"id": nid, "aid": payload["asset_id"], "qty": payload["qty"],
           "reason": payload["reason"], "desc": payload["description"],
           "cost": float(cost), "uid": user["user_id"]})
    await db.execute(text("UPDATE assets SET qty_ncm=qty_ncm+:q, qty_on_hand=qty_on_hand-:q WHERE id=:id"),
                     {"q": payload["qty"], "id": payload["asset_id"]})
    await audit.snapshot("assets", payload["asset_id"], "NCM_FLAG",
                         field_name="qty_ncm", new_value=payload["qty"])
    return {"id": nid}
