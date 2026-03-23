"""app/api/kits.py"""
import uuid as _uuid
import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.inventory import BOMValuationEngine, AuditService

router = APIRouter(prefix="/kits")


@router.get("")
async def list_kits(status: Optional[str] = None, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    where = "WHERE k.status = :s" if status else ""
    params = {"s": status} if status else {}
    result = await db.execute(text(f"""
        SELECT k.*, bh.name AS bom_name, bv.version,
               u.name AS assigned_to_name
        FROM kits k
        JOIN bom_versions bv ON bv.id = k.bom_version_id
        JOIN bom_headers bh ON bh.id = bv.bom_header_id
        LEFT JOIN users u ON u.id = k.assigned_to
        {where} ORDER BY k.created_at DESC
    """), params)
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("")
async def create_kit(payload: dict, request: Request,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    kid = str(_uuid.uuid4())
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)

    # Convert due_date string to date object if provided
    due = None
    if payload.get("due_date"):
        try:
            due = datetime.date.fromisoformat(payload["due_date"])
        except (ValueError, TypeError):
            due = None

    await db.execute(text("""
        INSERT INTO kits (id, job_number, bom_version_id, qty_to_build,
          assigned_to, due_date, notes, created_by)
        VALUES (:id, :job, :bom, :qty, :assign, :due, :notes, :uid)
    """), {
        "id": kid,
        "job": payload["job_number"],
        "bom": payload["bom_version_id"],
        "qty": payload.get("qty_to_build", 1),
        "assign": payload.get("assigned_to") or None,
        "due": due,
        "notes": payload.get("notes"),
        "uid": user["user_id"],
    })

    await audit.snapshot("kits", kid, "CREATE", new_value={"job_number": payload["job_number"]})
    await db.commit()
    return {"id": kid}


@router.patch("/{kit_id}")
async def update_kit(kit_id: str, payload: dict,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    fields, params = [], {"id": kit_id}
    for f in ["status", "notes", "assigned_to", "qty_to_build"]:
        if f in payload:
            fields.append(f"{f} = :{f}")
            params[f] = payload[f]
    # Handle due_date separately — convert to date
    if "due_date" in payload:
        fields.append("due_date = :due_date")
        try:
            params["due_date"] = datetime.date.fromisoformat(payload["due_date"]) if payload["due_date"] else None
        except (ValueError, TypeError):
            params["due_date"] = None
    if not fields:
        raise HTTPException(400, "No updatable fields")
    await db.execute(text(f"UPDATE kits SET {', '.join(fields)} WHERE id = :id"), params)
    await db.commit()
    return {"message": "Kit updated"}


@router.delete("/{kit_id}")
async def delete_kit(kit_id: str, request: Request,
                     db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    row = await db.execute(text("SELECT job_number, status FROM kits WHERE id = :id"), {"id": kit_id})
    kit = row.fetchone()
    if not kit:
        raise HTTPException(404, "Kit not found")
    if kit.status in ("KITTED", "IN_PROGRESS"):
        raise HTTPException(409, f"Cannot delete kit with status '{kit.status}'")
    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)
    await db.execute(text("DELETE FROM kit_issues WHERE kit_id = :id"), {"id": kit_id})
    await db.execute(text("DELETE FROM kits WHERE id = :id"), {"id": kit_id})
    await audit.snapshot("kits", kit_id, "DELETE", old_value={"job_number": kit.job_number})
    await db.commit()
    return {"message": "Kit deleted"}


@router.post("/{kit_id}/issue")
async def issue_kit(kit_id: str, request: Request,
                    db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    comps = await db.execute(text("""
        SELECT bc.asset_id, bc.qty_required, bc.uom, bc.id AS bom_component_id
        FROM kits k
        JOIN bom_components bc ON bc.bom_version_id = k.bom_version_id
        WHERE k.id = :kid
    """), {"kid": kit_id})
    components = comps.fetchall()

    for comp in components:
        stock = await db.execute(
            text("SELECT qty_on_hand, qty_wip FROM assets WHERE id = :id"),
            {"id": str(comp.asset_id)}
        )
        row = stock.fetchone()
        available = float(row.qty_on_hand) - float(row.qty_wip)
        if available < float(comp.qty_required):
            raise HTTPException(status_code=400,
                detail=f"Insufficient stock: need {comp.qty_required}, available {available}")

        await db.execute(text(
            "UPDATE assets SET qty_wip = qty_wip + :qty WHERE id = :id"
        ), {"qty": str(comp.qty_required), "id": str(comp.asset_id)})

        await db.execute(text("""
            INSERT INTO kit_issues (id, kit_id, asset_id, bom_component_id, std_qty, issued_qty, issued_by, issued_at)
            VALUES (:id, :kid, :aid, :bcid, :std, :iss, :uid, NOW())
        """), {
            "id": str(_uuid.uuid4()), "kid": kit_id, "aid": str(comp.asset_id),
            "bcid": str(comp.bom_component_id), "std": str(comp.qty_required),
            "iss": str(comp.qty_required), "uid": user["user_id"],
        })

    await db.execute(text("UPDATE kits SET status = 'KITTED', started_at = NOW() WHERE id = :id"), {"id": kit_id})
    await db.commit()
    return {"message": "Kit issued", "components_reserved": len(components)}


@router.post("/create-with-bom")
async def create_kit_with_bom(payload: dict, request: Request,
                               db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Single atomic endpoint: creates BOM header + version + kit in one transaction."""
    import datetime as _dt
    kid = str(_uuid.uuid4())
    ip = getattr(request.state, "client_ip", None) or None

    # Validate
    job_number = payload.get("job_number", "").strip()
    if not job_number:
        raise HTTPException(400, "Job number required")

    bom_version_id = payload.get("bom_version_id")  # existing BOM version
    
    # If creating a new BOM
    if not bom_version_id:
        bom_name = (payload.get("bom_name") or "").strip()
        bom_sku  = (payload.get("bom_sku")  or "").strip()
        components = payload.get("components", [])
        if not bom_name: raise HTTPException(400, "BOM name required")
        if not bom_sku:  raise HTTPException(400, "BOM SKU required")
        if not components: raise HTTPException(400, "At least one BOM component required")

        hid = str(_uuid.uuid4())
        vid = str(_uuid.uuid4())

        try:
            # 1. BOM header
            await db.execute(
                text("INSERT INTO bom_headers (id, name, sku, description, created_by) VALUES (:id, :name, :sku, :desc, :uid)"),
                {"id": hid, "name": bom_name, "sku": bom_sku, "desc": None, "uid": user["user_id"]}
            )
            # 2. BOM version
            await db.execute(text("""
                INSERT INTO bom_versions (id, bom_header_id, version, labor_rate_usd, build_time_hrs, overhead_pct, is_current, created_by)
                VALUES (:id, :hid, :ver, :lr, :bt, :oh, TRUE, :uid)
            """), {
                "id": vid, "hid": hid,
                "ver": payload.get("bom_version", "v1.0"),
                "lr": str(payload.get("labor_rate_usd", 0)),
                "bt": str(payload.get("build_time_hrs", 0)),
                "oh": str(payload.get("overhead_pct", 0)),
                "uid": user["user_id"],
            })
            # 3. Components
            for comp in components:
                await db.execute(text("""
                    INSERT INTO bom_components (id, bom_version_id, asset_id, qty_required, uom)
                    VALUES (:id, :vid, :aid, :qty, :uom)
                """), {
                    "id": str(_uuid.uuid4()), "vid": vid,
                    "aid": comp["asset_id"], "qty": comp["qty_required"], "uom": comp["uom"],
                })
            bom_version_id = vid
        except Exception as ex:
            await db.rollback()
            if "unique" in str(ex).lower():
                raise HTTPException(409, f"BOM SKU '{bom_sku}' already exists — use a different SKU.")
            raise HTTPException(500, f"Failed to create BOM: {str(ex)}")

    # Convert due_date
    due = None
    if payload.get("due_date"):
        try: due = _dt.date.fromisoformat(payload["due_date"])
        except (ValueError, TypeError): due = None

    # 4. Kit — same transaction
    await db.execute(text("""
        INSERT INTO kits (id, job_number, bom_version_id, qty_to_build, assigned_to, due_date, notes, created_by)
        VALUES (:id, :job, :bom, :qty, :assign, :due, :notes, :uid)
    """), {
        "id": kid, "job": job_number, "bom": bom_version_id,
        "qty": payload.get("qty_to_build", 1),
        "assign": payload.get("assigned_to") or None,
        "due": due, "notes": payload.get("notes"), "uid": user["user_id"],
    })

    await db.commit()

    # 5. Cut list (separate transaction — non-fatal if fails)
    cuts = payload.get("cuts", [])
    if cuts:
        try:
            clid = str(_uuid.uuid4())
            await db.execute(
                text("INSERT INTO cut_lists (id, name, kit_id, created_by) VALUES (:id, :name, :kid, :uid)"),
                {"id": clid, "name": job_number + " Cuts", "kid": kid, "uid": user["user_id"]}
            )
            for i, cut in enumerate(cuts):
                await db.execute(text("""
                    INSERT INTO cut_list_items (id, cut_list_id, description, material, length, qty, asset_id, sort_order)
                    VALUES (:id, :clid, :desc, :mat, :len, :qty, :aid, :sort)
                """), {
                    "id": str(_uuid.uuid4()), "clid": clid,
                    "desc": cut.get("description", ""), "mat": cut.get("material"),
                    "len": cut.get("length"), "qty": cut.get("qty", 1),
                    "aid": cut.get("asset_id"), "sort": i,
                })
            await db.commit()
        except Exception:
            await db.rollback()  # cuts failing is non-fatal

    return {"id": kid, "message": "Kit created"}


@router.get("/{kit_id}/components")
async def kit_components(kit_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Return BOM components with current stock levels for the build sheet."""
    result = await db.execute(text("""
        SELECT
            a.id AS asset_id,
            a.name, a.sku, a.uom,
            a.qty_on_hand,
            a.qty_wip,
            (a.qty_on_hand - a.qty_wip) AS qty_available,
            bc.qty_required,
            a.location_id,
            l.code AS location,
            a.image_url,
            ki.issued_qty,
            ki.consumed_qty
        FROM kits k
        JOIN bom_versions bv ON bv.id = k.bom_version_id
        JOIN bom_components bc ON bc.bom_version_id = bv.id
        JOIN assets a ON a.id = bc.asset_id
        LEFT JOIN locations l ON l.id = a.location_id
        LEFT JOIN kit_issues ki ON ki.kit_id = k.id AND ki.asset_id = a.id
        WHERE k.id = :kid
        ORDER BY a.name
    """), {"kid": kit_id})
    return [dict(r._mapping) for r in result.fetchall()]


@router.post("/{kit_id}/complete")
async def complete_kit(kit_id: str, request: Request,
                       db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Mark kit as complete — deducts consumed qty from on-hand, clears WIP."""
    kit_row = await db.execute(text("SELECT job_number, status, bom_version_id FROM kits WHERE id = :id"), {"id": kit_id})
    kit = kit_row.fetchone()
    if not kit:
        raise HTTPException(404, "Kit not found")
    if kit.status not in ("KITTED", "IN_PROGRESS"):
        raise HTTPException(400, f"Kit must be KITTED or IN_PROGRESS to complete (current: {kit.status})")

    issues = await db.execute(text("""
        SELECT id, asset_id, issued_qty FROM kit_issues WHERE kit_id = :kid
    """), {"kid": kit_id})

    for issue in issues.fetchall():
        consumed = float(issue.issued_qty)
        await db.execute(text("""
            UPDATE assets SET
                qty_on_hand = qty_on_hand - :consumed,
                qty_wip = GREATEST(qty_wip - :issued, 0)
            WHERE id = :id
        """), {"consumed": consumed, "issued": consumed, "id": str(issue.asset_id)})
        await db.execute(text("""
            UPDATE kit_issues SET consumed_qty = :c, returned_qty = 0 WHERE id = :id
        """), {"c": consumed, "id": str(issue.id)})

    await db.execute(text("""
        UPDATE kits SET status = 'COMPLETE', completed_at = NOW() WHERE id = :id
    """), {"id": kit_id})

    ip = getattr(request.state, "client_ip", None) or None
    audit = AuditService(db, user["user_id"], ip)
    await audit.snapshot("kits", kit_id, "STATUS_CHANGE",
                         old_value={"status": kit.status}, new_value={"status": "COMPLETE"})
    await db.commit()
    return {"message": "Kit marked complete"}


@router.get("/{kit_id}/cutlist")
async def kit_cutlist(kit_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Return cut list items and kit/BOM notes for the build sheet."""
    # Get kit notes + BOM version notes
    kit_row = await db.execute(text("""
        SELECT k.notes AS kit_notes, k.job_number,
               bv.version_notes AS bom_notes,
               bh.name AS bom_name
        FROM kits k
        JOIN bom_versions bv ON bv.id = k.bom_version_id
        JOIN bom_headers bh ON bh.id = bv.bom_header_id
        WHERE k.id = :id
    """), {"id": kit_id})
    kit = kit_row.fetchone()
    if not kit:
        raise HTTPException(404, "Kit not found")

    # Get cut list items
    items_row = await db.execute(text("""
        SELECT cli.*, a.name AS asset_name, a.sku, cl.notes AS list_notes
        FROM cut_lists cl
        JOIN cut_list_items cli ON cli.cut_list_id = cl.id
        LEFT JOIN assets a ON a.id = cli.asset_id
        WHERE cl.kit_id = :kid
        ORDER BY cli.asset_id, cli.sort_order
    """), {"kid": kit_id})
    items = [dict(r._mapping) for r in items_row.fetchall()]

    # Auto-total: group by asset_id, sum qty * length_numeric in each UOM
    totals = {}
    for item in items:
        aid = item.get("asset_id")
        if not aid or not item.get("length_numeric"):
            continue
        key = (aid, item.get("length_uom", "IN"))
        if key not in totals:
            totals[key] = {
                "asset_id": aid,
                "asset_name": item.get("asset_name", ""),
                "sku": item.get("sku", ""),
                "length_uom": item.get("length_uom", "IN"),
                "total_length": 0.0,
                "cut_count": 0,
            }
        totals[key]["total_length"] += float(item.get("length_numeric", 0)) * float(item.get("qty", 1))
        totals[key]["cut_count"] += int(item.get("qty", 1))

    return {
        "kit_notes": kit.kit_notes,
        "bom_notes": kit.bom_notes,
        "bom_name": kit.bom_name,
        "items": items,
        "totals": list(totals.values()),
    }
