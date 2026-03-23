"""app/api/cut_lists.py"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
import uuid as _uuid

router = APIRouter(prefix="/cut-lists")


@router.get("")
async def list_cut_lists(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("""
        SELECT cl.*, k.job_number AS kit_job_number,
               (SELECT COUNT(*) FROM cut_list_items cli WHERE cli.cut_list_id = cl.id) AS item_count
        FROM cut_lists cl
        LEFT JOIN kits k ON k.id = cl.kit_id
        ORDER BY cl.created_at DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/{cut_list_id}")
async def get_cut_list(cut_list_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(text("SELECT * FROM cut_lists WHERE id = :id"), {"id": cut_list_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(404, "Cut list not found")
    cl = dict(row._mapping)
    items = await db.execute(text("""
        SELECT cli.*, a.name AS asset_name, a.sku
        FROM cut_list_items cli
        LEFT JOIN assets a ON a.id = cli.asset_id
        WHERE cli.cut_list_id = :id
        ORDER BY cli.sort_order
    """), {"id": cut_list_id})
    cl["items"] = [dict(r._mapping) for r in items.fetchall()]
    return cl


def _insert_items(items, clid):
    """Return list of param dicts for cut list items."""
    rows = []
    for i, item in enumerate(items):
        length_num = None
        try:
            length_num = float(item.get("length_numeric") or item.get("length") or 0) or None
        except (ValueError, TypeError):
            length_num = None
        rows.append({
            "id": str(_uuid.uuid4()), "clid": clid,
            "desc": item.get("description", ""),
            "mat": item.get("material"),
            "len": str(item.get("length")) if item.get("length") else None,
            "len_num": length_num,
            "len_uom": item.get("length_uom") or "IN",
            "qty": item.get("qty", 1),
            "aid": item.get("asset_id"),
            "sort": i,
        })
    return rows


@router.post("", status_code=201)
async def create_cut_list(payload: dict, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Cut list name required")
    clid = str(_uuid.uuid4())
    await db.execute(text("""
        INSERT INTO cut_lists (id, name, kit_id, notes, created_by)
        VALUES (:id, :name, :kid, :notes, :uid)
    """), {"id": clid, "name": name, "kid": payload.get("kit_id"),
           "notes": payload.get("notes"), "uid": user["user_id"]})
    for row in _insert_items(payload.get("items", []), clid):
        await db.execute(text("""
            INSERT INTO cut_list_items
              (id, cut_list_id, description, material, length, length_numeric, length_uom, qty, asset_id, sort_order)
            VALUES (:id, :clid, :desc, :mat, :len, :len_num, :len_uom, :qty, :aid, :sort)
        """), row)
    await db.commit()
    return {"id": clid, "message": "Cut list created"}


@router.patch("/{cut_list_id}")
async def update_cut_list(cut_list_id: str, payload: dict,
                           db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    fields, params = [], {"id": cut_list_id}
    if "name"   in payload: fields.append("name = :name");     params["name"]   = payload["name"]
    if "kit_id" in payload: fields.append("kit_id = :kid");    params["kid"]    = payload["kit_id"]
    if "notes"  in payload: fields.append("notes = :notes");   params["notes"]  = payload["notes"]
    if fields:
        await db.execute(text(f"UPDATE cut_lists SET {', '.join(fields)} WHERE id = :id"), params)
    if "items" in payload:
        await db.execute(text("DELETE FROM cut_list_items WHERE cut_list_id = :id"), {"id": cut_list_id})
        for row in _insert_items(payload["items"], cut_list_id):
            await db.execute(text("""
                INSERT INTO cut_list_items
                  (id, cut_list_id, description, material, length, length_numeric, length_uom, qty, asset_id, sort_order)
                VALUES (:id, :clid, :desc, :mat, :len, :len_num, :len_uom, :qty, :aid, :sort)
            """), row)
    await db.commit()
    return {"message": "Cut list updated"}


@router.delete("/{cut_list_id}")
async def delete_cut_list(cut_list_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    await db.execute(text("DELETE FROM cut_list_items WHERE cut_list_id = :id"), {"id": cut_list_id})
    await db.execute(text("DELETE FROM cut_lists WHERE id = :id"), {"id": cut_list_id})
    await db.commit()
    return {"message": "Cut list deleted"}
