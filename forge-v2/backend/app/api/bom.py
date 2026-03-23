"""app/api/bom.py"""
import uuid as _uuid
from typing import Optional, List
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.inventory import BOMValuationEngine, AuditService

router = APIRouter(prefix="/bom")


class BOMVersionCreate(BaseModel):
    bom_header_id: str
    version: str
    version_notes: Optional[str] = None
    labor_rate_usd: Decimal = Decimal("0")
    build_time_hrs: Decimal = Decimal("0")
    overhead_pct: Decimal = Decimal("0")
    components: List[dict] = []


@router.get("")
async def list_boms(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT bh.id, bh.name, bh.sku, bh.is_active,
               bv.id AS version_id, bv.version, bv.is_current, bv.total_cogs, bv.created_at
        FROM bom_headers bh
        LEFT JOIN bom_versions bv ON bv.bom_header_id = bh.id AND bv.is_current = TRUE
        ORDER BY bh.name
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/{bom_id}/versions")
async def list_versions(bom_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT bv.*, COUNT(bc.id) AS component_count
        FROM bom_versions bv
        LEFT JOIN bom_components bc ON bc.bom_version_id = bv.id
        WHERE bv.bom_header_id = :id
        GROUP BY bv.id
        ORDER BY bv.created_at DESC
    """), {"id": bom_id})
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("/headers", status_code=201)
async def create_bom_header(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    sku  = (payload.get("sku")  or "").strip()
    if not name: raise HTTPException(400, "BOM name required")
    if not sku:  raise HTTPException(400, "Finished-good SKU required")
    hid = str(_uuid.uuid4())
    try:
        await db.execute(
            text("INSERT INTO bom_headers (id, name, sku, description, created_by) VALUES (:id, :name, :sku, :desc, :uid)"),
            {"id": hid, "name": name, "sku": sku, "desc": payload.get("description"), "uid": user["user_id"]}
        )
        await db.commit()
    except Exception as ex:
        await db.rollback()
        if "unique" in str(ex).lower():
            raise HTTPException(409, f"BOM SKU '{sku}' already exists — use a different SKU.")
        raise HTTPException(500, "Failed to create BOM header")
    return {"id": hid, "name": name, "sku": sku}


@router.post("/versions")
async def create_version(payload: BOMVersionCreate, request: Request,
                         db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    vid = str(_uuid.uuid4())
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)

    await db.execute(text(
        "UPDATE bom_versions SET is_current = FALSE WHERE bom_header_id = :hid"
    ), {"hid": payload.bom_header_id})

    await db.execute(text("""
        INSERT INTO bom_versions (id, bom_header_id, version, version_notes,
          labor_rate_usd, build_time_hrs, overhead_pct, is_current, created_by)
        VALUES (:id, :hid, :ver, :notes, :lr, :bt, :oh, TRUE, :uid)
    """), {
        "id": vid, "hid": payload.bom_header_id, "ver": payload.version,
        "notes": payload.version_notes, "lr": str(payload.labor_rate_usd),
        "bt": str(payload.build_time_hrs), "oh": str(payload.overhead_pct),
        "uid": user["user_id"],
    })

    for comp in payload.components:
        cid = str(_uuid.uuid4())
        await db.execute(text("""
            INSERT INTO bom_components (id, bom_version_id, asset_id, qty_required, uom, notes)
            VALUES (:id, :vid, :aid, :qty, :uom, :notes)
        """), {
            "id": cid, "vid": vid, "aid": comp["asset_id"],
            "qty": comp["qty_required"], "uom": comp["uom"],
            "notes": comp.get("notes"),
        })

    await audit.snapshot("bom_versions", vid, "CREATE", new_value={"version": payload.version})
    await db.commit()
    return {"id": vid}


@router.get("/versions/{version_id}/cogs")
async def get_cogs(version_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    engine = BOMValuationEngine(db)
    return await engine.calculate_cogs(version_id)


@router.delete("/headers/{bom_id}")
async def delete_bom_header(bom_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    check = await db.execute(text("""
        SELECT COUNT(*) FROM kits k
        JOIN bom_versions bv ON bv.id = k.bom_version_id
        WHERE bv.bom_header_id = :id
    """), {"id": bom_id})
    if check.scalar() > 0:
        raise HTTPException(409, "Cannot delete: kits reference this BOM")
    await db.execute(text("DELETE FROM bom_components WHERE bom_version_id IN (SELECT id FROM bom_versions WHERE bom_header_id = :id)"), {"id": bom_id})
    await db.execute(text("DELETE FROM bom_versions WHERE bom_header_id = :id"), {"id": bom_id})
    await db.execute(text("DELETE FROM bom_headers WHERE id = :id"), {"id": bom_id})
    await db.commit()
    return {"message": "BOM deleted"}


@router.delete("/versions/{version_id}")
async def delete_bom_version(version_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    check = await db.execute(text("SELECT COUNT(*) FROM kits WHERE bom_version_id = :id"), {"id": version_id})
    if check.scalar() > 0:
        raise HTTPException(409, "Cannot delete: kits use this BOM version")
    await db.execute(text("DELETE FROM bom_components WHERE bom_version_id = :id"), {"id": version_id})
    await db.execute(text("DELETE FROM bom_versions WHERE id = :id"), {"id": version_id})
    await db.commit()
    return {"message": "BOM version deleted"}
