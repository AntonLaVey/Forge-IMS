"""app/api/assets.py"""
import json
import uuid as _uuid
import decimal as _dec
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from decimal import Decimal

from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.inventory import AuditService, ReorderEngine

router = APIRouter(prefix="/assets")


class AssetCreate(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    category_id: UUID
    vendor_id: Optional[UUID] = None
    location_id: Optional[UUID] = None
    uom: str = "EACH"
    qty_on_hand: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    reorder_point: Optional[Decimal] = None
    reorder_qty: Optional[Decimal] = None
    safety_stock: Decimal = Decimal("0")
    is_linear: bool = False
    attributes: dict = {}
    image_url: Optional[str] = None


class AssetUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    location_id: Optional[UUID] = None
    uom: Optional[str] = None
    unit_cost: Optional[Decimal] = None
    reorder_point: Optional[Decimal] = None
    reorder_qty: Optional[Decimal] = None
    safety_stock: Optional[Decimal] = None
    attributes: Optional[dict] = None
    image_url: Optional[str] = None


class AdjustQty(BaseModel):
    delta: Decimal
    reason: str


@router.get("")
async def list_assets(
    q: Optional[str] = Query(None),
    category_id: Optional[UUID] = None,
    vendor_id: Optional[UUID] = None,
    status: Optional[str] = None,
    below_rop: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    offset = (page - 1) * page_size
    conditions = ["1=1"]
    params: dict = {"limit": page_size, "offset": offset}

    if q:
        conditions.append("(a.name ILIKE :q OR a.sku ILIKE :q)")
        params["q"] = f"%{q}%"

    if category_id:
        conditions.append("a.category_id = :cat")
        params["cat"] = str(category_id)

    if vendor_id:
        conditions.append("a.vendor_id = :ven")
        params["ven"] = str(vendor_id)

    if status:
        conditions.append("a.status = :status")
        params["status"] = status

    if below_rop:
        conditions.append("a.reorder_point IS NOT NULL AND (a.qty_on_hand - a.qty_wip) <= a.reorder_point")

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
          a.id, a.sku, a.name, a.status, a.uom,
          a.qty_on_hand, a.qty_wip, a.qty_ncm, a.qty_remnant,
          (a.qty_on_hand - a.qty_wip) AS qty_available,
          a.unit_cost,
          (a.qty_on_hand * a.unit_cost) AS total_value,
          a.reorder_point, a.is_linear, a.attributes,
          c.name AS category, v.name AS vendor, l.code AS location,
          a.updated_at,
          COUNT(*) OVER() AS total_count
        FROM assets a
        LEFT JOIN categories c ON c.id = a.category_id
        LEFT JOIN vendors v ON v.id = a.vendor_id
        LEFT JOIN locations l ON l.id = a.location_id
        WHERE {where}
        ORDER BY a.name
        LIMIT :limit OFFSET :offset
    """
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    total = rows[0].total_count if rows else 0
    return {
        "items": [dict(r._mapping) for r in rows],
        "total": total,
        "page": page,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/{asset_id}")
async def get_asset(asset_id: UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text("""
            SELECT a.*, c.name AS category, v.name AS vendor, l.code AS location
            FROM assets a
            LEFT JOIN categories c ON c.id = a.category_id
            LEFT JOIN vendors v ON v.id = a.vendor_id
            LEFT JOIN locations l ON l.id = a.location_id
            WHERE a.id = :id
        """),
        {"id": str(asset_id)},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Material not found")
    return dict(row._mapping)


@router.post("", status_code=201)
async def create_asset(
    payload: AssetCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    asset_id = str(_uuid.uuid4())
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)

    await db.execute(
        text("""
            INSERT INTO assets (
              id, sku, name, description, category_id, vendor_id, location_id,
              uom, qty_on_hand, unit_cost, reorder_point, reorder_qty,
              safety_stock, is_linear, attributes, image_url, created_by
            ) VALUES (
              :id, :sku, :name, :desc, :cat, :ven, :loc,
              :uom, :qty, :cost, :rop, :roq,
              :ss, :lin, :attrs, :img, :uid
            )
        """),
        {
            "id": asset_id,
            "sku": payload.sku,
            "name": payload.name,
            "desc": payload.description,
            "cat": str(payload.category_id),
            "ven": str(payload.vendor_id) if payload.vendor_id else None,
            "loc": str(payload.location_id) if payload.location_id else None,
            "uom": payload.uom,
            "qty": str(payload.qty_on_hand),
            "cost": str(payload.unit_cost),
            "rop": str(payload.reorder_point) if payload.reorder_point else None,
            "roq": str(payload.reorder_qty) if payload.reorder_qty else None,
            "ss": str(payload.safety_stock),
            "lin": payload.is_linear,
            "attrs": json.dumps(payload.attributes),
            "img": payload.image_url,
            "uid": user["user_id"],
        },
    )
    await audit.snapshot("assets", asset_id, "CREATE", new_value={"sku": payload.sku, "name": payload.name})
    await db.commit()
    return {"id": asset_id, "message": "Material created"}


@router.patch("/{asset_id}")
async def update_asset(
    asset_id: UUID,
    payload: AssetUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)

    before = await db.execute(text("SELECT * FROM assets WHERE id = :id"), {"id": str(asset_id)})
    before_row = before.fetchone()
    if not before_row:
        raise HTTPException(status_code=404, detail="Material not found")

    updates = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not updates:
        return {"message": "No changes"}

    # Sanitize values for SQLAlchemy
    clean = {}
    for k, v in updates.items():
        if isinstance(v, (_dec.Decimal, UUID)):
            clean[k] = str(v)
        elif isinstance(v, dict):
            clean[k] = json.dumps(v)
        else:
            clean[k] = v

    set_clauses = ", ".join(f"{k} = :{k}" for k in clean)
    clean["id"] = str(asset_id)
    await db.execute(text(f"UPDATE assets SET {set_clauses} WHERE id = :id"), clean)

    old_dict = dict(before_row._mapping)
    await audit.snapshot_diff("assets", str(asset_id), old_dict, {**old_dict, **clean})
    await db.commit()
    return {"message": "Updated"}


@router.post("/{asset_id}/adjust")
async def adjust_quantity(
    asset_id: UUID,
    payload: AdjustQty,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)

    before = await db.execute(text("SELECT qty_on_hand FROM assets WHERE id = :id"), {"id": str(asset_id)})
    row = before.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Material not found")

    new_qty = float(row.qty_on_hand) + float(payload.delta)
    if new_qty < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot go negative")

    await db.execute(text("UPDATE assets SET qty_on_hand = :q WHERE id = :id"), {"q": new_qty, "id": str(asset_id)})
    await audit.snapshot("assets", str(asset_id), "UPDATE",
                         field_name="qty_on_hand", old_value=float(row.qty_on_hand), new_value=new_qty)
    await db.commit()
    return {"qty_on_hand": new_qty}


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    result = await db.execute(text("SELECT name, status FROM assets WHERE id = :id"), {"id": str(asset_id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Material not found")
    if row.status == "WIP":
        raise HTTPException(status_code=409, detail="Cannot delete material currently in WIP")
    await db.execute(text("UPDATE assets SET status = 'SCRAPPED' WHERE id = :id"), {"id": str(asset_id)})
    await db.commit()
    return {"message": "Material scrapped"}
