"""app/api/procurement.py"""
import uuid as _uuid
import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, require_admin

router = APIRouter(prefix="/procurement")


# ── Vendors ──────────────────────────────────────────────────

@router.get("/vendors")
async def list_vendors(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT id, name, contact_email, contact_phone, account_number, notes, is_active
        FROM vendors WHERE is_active=TRUE ORDER BY name
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("/vendors", status_code=201)
async def create_vendor(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Vendor name required")
    vid = str(_uuid.uuid4())
    await db.execute(text("""
        INSERT INTO vendors (id, name, contact_email, contact_phone, account_number, notes)
        VALUES (:id, :name, :email, :phone, :acct, :notes)
    """), {
        "id": vid, "name": name,
        "email": payload.get("contact_email") or None,
        "phone": payload.get("contact_phone") or None,
        "acct":  payload.get("account_number") or None,
        "notes": payload.get("notes") or None,
    })
    await db.commit()
    return {"id": vid, "name": name, "message": "Vendor created"}


@router.patch("/vendors/{vendor_id}")
async def update_vendor(vendor_id: str, payload: dict,
                        db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    fields, params = [], {"id": vendor_id}
    for f in ["name", "contact_email", "contact_phone", "account_number", "notes"]:
        if f in payload:
            fields.append(f"{f} = :{f}"); params[f] = payload[f] or None
    if not fields:
        raise HTTPException(400, "No fields to update")
    await db.execute(text(f"UPDATE vendors SET {', '.join(fields)} WHERE id = :id"), params)
    await db.commit()
    return {"message": "Vendor updated"}


@router.delete("/vendors/{vendor_id}")
async def delete_vendor(vendor_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    await db.execute(text("UPDATE vendors SET is_active=FALSE WHERE id=:id"), {"id": vendor_id})
    await db.commit()
    return {"message": "Vendor deactivated"}


# ── Requests ─────────────────────────────────────────────────

@router.get("/requests")
async def list_requests(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT r.*, u.name AS requester, rv.name AS reviewer_name
        FROM requests r JOIN users u ON u.id=r.requested_by
        LEFT JOIN users rv ON rv.id=r.reviewed_by
        ORDER BY r.requested_at DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("/requests", status_code=201)
async def create_request(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    title = (payload.get("title") or "").strip()
    desc  = (payload.get("description") or "").strip()
    if not title: raise HTTPException(400, "Title required")
    if not desc:  raise HTTPException(400, "Description required")

    # Convert needed_by string to date
    needed = None
    if payload.get("needed_by"):
        try: needed = datetime.date.fromisoformat(payload["needed_by"])
        except (ValueError, TypeError): needed = None

    rid = str(_uuid.uuid4())
    await db.execute(text("""
        INSERT INTO requests (id,title,description,qty_requested,estimated_cost,requested_by,needed_by,priority)
        VALUES (:id,:title,:desc,:qty,:cost,:uid,:needed,:pri)
    """), {
        "id": rid, "title": title, "desc": desc,
        "qty":    float(payload["qty_requested"]) if payload.get("qty_requested") else None,
        "cost":   float(payload["estimated_cost"]) if payload.get("estimated_cost") else None,
        "uid":    user["user_id"],
        "needed": needed,
        "pri":    int(payload.get("priority") or 2),
    })
    await db.commit()
    return {"id": rid, "message": "Request submitted"}


@router.delete("/requests/{req_id}")
async def delete_request(req_id: str, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    await db.execute(text("DELETE FROM requests WHERE id = :id"), {"id": req_id})
    await db.commit()
    return {"message": "Request deleted"}


@router.patch("/requests/{req_id}/review")
async def review_request(req_id: str, payload: dict,
                          db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    await db.execute(text("""
        UPDATE requests SET status=:s, reviewed_by=:uid, review_notes=:notes, reviewed_at=NOW()
        WHERE id=:id
    """), {"s": payload["status"], "uid": user["user_id"], "notes": payload.get("notes"), "id": req_id})
    await db.commit()
    return {"message": "Updated"}


# ── Purchase Orders ───────────────────────────────────────────

@router.get("/pos")
async def list_pos(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT po.*, v.name AS vendor_name FROM purchase_orders po
        JOIN vendors v ON v.id=po.vendor_id ORDER BY po.created_at DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("/pos", status_code=201)
async def create_po(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    vendor_id = payload.get("vendor_id")
    if not vendor_id: raise HTTPException(400, "Vendor required")
    import secrets
    pid    = str(_uuid.uuid4())
    po_num = f"PO-{secrets.token_hex(3).upper()}"
    await db.execute(text("""
        INSERT INTO purchase_orders (id,po_number,vendor_id,status,budget_category,notes,created_by)
        VALUES (:id,:num,:ven,'DRAFT',:cat,:notes,:uid)
    """), {
        "id": pid, "num": po_num, "ven": vendor_id,
        "cat":   payload.get("budget_category") or None,
        "notes": payload.get("notes") or None,
        "uid":   user["user_id"],
    })
    await db.commit()
    return {"id": pid, "po_number": po_num, "message": "PO created"}
