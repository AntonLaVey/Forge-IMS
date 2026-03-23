"""Vendor endpoints."""
from __future__ import annotations

import uuid as _uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.inventory import AuditService, MassPriceUpdater

router = APIRouter(prefix="/vendors")


class VendorCreate(BaseModel):
    name: str
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    account_number: Optional[str] = None
    notes: Optional[str] = None


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    account_number: Optional[str] = None
    notes: Optional[str] = None


@router.get("")
async def list_vendors(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text(
            """
            SELECT v.*, COUNT(a.id) AS material_count
            FROM vendors v
            LEFT JOIN assets a ON a.vendor_id = v.id
            WHERE v.is_active = TRUE
            GROUP BY v.id
            ORDER BY v.name
            """
        )
    )
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("", status_code=201)
async def create_vendor(payload: VendorCreate, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Vendor name required")
    vid = str(_uuid.uuid4())
    await db.execute(
        text(
            """
            INSERT INTO vendors (id, name, contact_name, contact_email, contact_phone, account_number, notes)
            VALUES (:id, :name, :cname, :email, :phone, :acct, :notes)
            """
        ),
        {
            "id": vid,
            "name": name,
            "cname": payload.contact_name,
            "email": payload.contact_email,
            "phone": payload.contact_phone,
            "acct": payload.account_number,
            "notes": payload.notes,
        },
    )
    await db.commit()
    return {"id": vid, "name": name, "message": "Vendor created"}


@router.patch("/{vendor_id}")
async def update_vendor(
    vendor_id: str,
    payload: VendorUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    fields, params = [], {"id": vendor_id}
    if payload.name is not None:
        fields.append("name = :name")
        params["name"] = payload.name
    if payload.contact_name is not None:
        fields.append("contact_name = :cname")
        params["cname"] = payload.contact_name
    if payload.contact_email is not None:
        fields.append("contact_email = :email")
        params["email"] = payload.contact_email
    if payload.contact_phone is not None:
        fields.append("contact_phone = :phone")
        params["phone"] = payload.contact_phone
    if payload.account_number is not None:
        fields.append("account_number = :acct")
        params["acct"] = payload.account_number
    if payload.notes is not None:
        fields.append("notes = :notes")
        params["notes"] = payload.notes
    if not fields:
        raise HTTPException(400, "No fields to update")
    await db.execute(text(f"UPDATE vendors SET {', '.join(fields)} WHERE id = :id"), params)
    await db.commit()
    return {"message": "Vendor updated"}


@router.post("/mass-price-update")
async def mass_price_update(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    audit = AuditService(db, user["user_id"])
    updater = MassPriceUpdater(db, audit)
    if payload.get("type") == "percentage":
        count = await updater.apply_percentage(
            payload["value"],
            category_id=payload.get("category_id"),
            vendor_id=payload.get("vendor_id"),
        )
    else:
        count = await updater.apply_flat_rate(
            payload["value"],
            category_id=payload.get("category_id"),
            vendor_id=payload.get("vendor_id"),
        )
    return {"updated_count": count}


@router.delete("/{vendor_id}")
async def delete_vendor(vendor_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    check = await db.execute(text("SELECT COUNT(*) FROM assets WHERE vendor_id = :id"), {"id": vendor_id})
    if check.scalar() > 0:
        raise HTTPException(409, "Cannot delete: materials are sourced from this vendor. Reassign them first.")
    await db.execute(text("UPDATE vendors SET is_active = FALSE WHERE id = :id"), {"id": vendor_id})
    await db.commit()
    return {"message": "Vendor removed"}
