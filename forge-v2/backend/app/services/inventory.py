"""
app/services/inventory.py
Core inventory, linear cutting, BOM valuation, and audit services.
"""
from __future__ import annotations
import json
import uuid
from decimal import Decimal
from typing import Optional, Any
from datetime import datetime

from sqlalchemy import text, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import validate_session


# ══════════════════════════════════════════════════════════════
# AUDIT SERVICE
# ══════════════════════════════════════════════════════════════

class AuditService:
    def __init__(self, db: AsyncSession, user_id: str, ip=None):
        self.db = db
        self.user_id = user_id
        self.ip = ip

    async def snapshot(
        self,
        table_name: str,
        record_id: str,
        action: str,
        field_name: Optional[str] = None,
        old_value: Any = None,
        new_value: Any = None,
        session_id: Optional[str] = None,
    ):
        """Write one immutable audit record."""
        await self.db.execute(
            text("""
                INSERT INTO audit_snapshots
                  (table_name, record_id, action, field_name,
                   old_value, new_value, performed_by, ip_address, session_id)
                VALUES
                  (:t, :r, :a, :f, :ov, :nv, :u, :ip, :sid)
            """),
            {
                "t": table_name, "r": record_id, "a": action,
                "f": field_name,
                "ov": json.dumps(old_value, default=str) if old_value is not None else None,
                "nv": json.dumps(new_value, default=str) if new_value is not None else None,
                "u": self.user_id, "ip": self.ip if self.ip else None, "sid": session_id,
            },
        )

    async def snapshot_diff(
        self, table_name: str, record_id: str,
        old_dict: dict, new_dict: dict
    ):
        """Snapshot every changed field individually."""
        for key in new_dict:
            old_val = old_dict.get(key)
            new_val = new_dict.get(key)
            if old_val != new_val:
                await self.snapshot(
                    table_name, record_id, "UPDATE",
                    field_name=key,
                    old_value=old_val,
                    new_value=new_val,
                )

    async def rollback_field(self, audit_snapshot_id: str) -> bool:
        """
        Revert a specific field to its old_value and mark the
        snapshot as rolled back.
        """
        result = await self.db.execute(
            text("""
                SELECT table_name, record_id, field_name, old_value, is_rolled_back
                FROM audit_snapshots WHERE id = :id
            """),
            {"id": audit_snapshot_id},
        )
        row = result.fetchone()
        if not row or row.is_rolled_back:
            return False

        # Apply rollback via dynamic SQL (field name is internal — safe)
        await self.db.execute(
            text(f"UPDATE {row.table_name} SET {row.field_name} = :v WHERE id = :id"),
            {"v": json.loads(row.old_value), "id": row.record_id},
        )

        # Mark original snapshot as rolled back + create rollback audit
        await self.db.execute(
            text("UPDATE audit_snapshots SET is_rolled_back = TRUE WHERE id = :id"),
            {"id": audit_snapshot_id},
        )
        await self.snapshot(
            row.table_name, row.record_id, "ROLLBACK",
            field_name=row.field_name,
            old_value=json.loads(row.old_value),
            new_value=json.loads(row.old_value),
            session_id=audit_snapshot_id,
        )
        return True


# ══════════════════════════════════════════════════════════════
# LINEAR CUTTING ENGINE (Creform Module)
# ══════════════════════════════════════════════════════════════

class LinearCuttingEngine:
    """
    Handles all math for linear stock (tubing, extrusions, bar stock).
    All internal calculations in inches.
    """

    MM_PER_INCH = Decimal("25.4")

    @classmethod
    def plan_cut(
        cls,
        stock_length_in: Decimal,
        piece_length_in: Decimal,
        kerf_mm: Decimal,
        qty_sticks: int = 1,
    ) -> dict:
        """
        Calculate how many pieces per stick, total yield,
        remnant, and kerf waste.
        """
        kerf_in = kerf_mm / cls.MM_PER_INCH
        # pieces per stick = floor((stock - remnant_threshold) / (piece + kerf))
        pieces_per_stick = int(stock_length_in / (piece_length_in + kerf_in))
        if pieces_per_stick == 0:
            raise ValueError(
                f"Stock length {stock_length_in}\" is shorter than "
                f"piece length {piece_length_in}\""
            )

        total_cut_length = pieces_per_stick * piece_length_in
        total_kerf_in = (pieces_per_stick - 1) * kerf_in  # n-1 cuts per stick
        # First cut assumed from existing end — no lead kerf
        remnant_in = stock_length_in - total_cut_length - total_kerf_in

        total_pieces = pieces_per_stick * qty_sticks
        total_remnant_in = remnant_in * qty_sticks
        total_kerf_waste_in = total_kerf_in * qty_sticks

        return {
            "pieces_per_stick": pieces_per_stick,
            "total_pieces": total_pieces,
            "kerf_per_stick_in": float(total_kerf_in),
            "remnant_per_stick_in": float(remnant_in),
            "total_remnant_in": float(total_remnant_in),
            "total_kerf_waste_in": float(total_kerf_waste_in),
            "material_yield_pct": float(
                (total_cut_length / stock_length_in) * 100
            ),
        }

    @classmethod
    def categorize_remnant(
        cls,
        remnant_in: Decimal,
        min_usable_in: Decimal = Decimal("6"),
    ) -> str:
        """
        Remnant >= min_usable_in → USABLE_REMNANT (re-enter inventory)
        Else → SCRAP
        """
        return "USABLE_REMNANT" if remnant_in >= min_usable_in else "SCRAP"

    @classmethod
    def convert_uom(
        cls,
        qty: Decimal,
        from_uom: str,
        to_uom: str,
        factor: Decimal,
    ) -> Decimal:
        """Generic UoM conversion using asset's conversion_factor."""
        conversions = {
            ("FT", "IN"): Decimal("12"),
            ("M", "MM"): Decimal("1000"),
            ("M", "CM"): Decimal("100"),
            ("KG", "LB"): Decimal("2.20462"),
        }
        key = (from_uom.upper(), to_uom.upper())
        conv = conversions.get(key, factor)
        return qty * conv


# ══════════════════════════════════════════════════════════════
# BOM VALUATION ENGINE
# ══════════════════════════════════════════════════════════════

class BOMValuationEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def calculate_cogs(self, bom_version_id: str) -> dict:
        """Full COGS breakdown for a BOM version."""
        result = await self.db.execute(
            text("""
                SELECT
                  bv.labor_rate_usd, bv.build_time_hrs, bv.overhead_pct,
                  COALESCE(SUM(bc.qty_required * a.unit_cost), 0) AS mat_cost,
                  COUNT(bc.id) AS part_count,
                  jsonb_agg(jsonb_build_object(
                    'sku', a.sku, 'name', a.name,
                    'qty', bc.qty_required, 'uom', bc.uom,
                    'unit_cost', a.unit_cost,
                    'line_cost', bc.qty_required * a.unit_cost
                  )) AS line_items
                FROM bom_versions bv
                LEFT JOIN bom_components bc ON bc.bom_version_id = bv.id
                LEFT JOIN assets a ON a.id = bc.asset_id
                WHERE bv.id = :vid
                GROUP BY bv.labor_rate_usd, bv.build_time_hrs, bv.overhead_pct
            """),
            {"vid": bom_version_id},
        )
        row = result.fetchone()
        if not row:
            return {}

        mat = Decimal(str(row.mat_cost))
        labor = Decimal(str(row.labor_rate_usd)) * Decimal(str(row.build_time_hrs))
        overhead_mult = Decimal("1") + Decimal(str(row.overhead_pct))
        total_cogs = (mat + labor) * overhead_mult

        return {
            "material_cost": float(mat),
            "labor_cost": float(labor),
            "overhead_pct": float(row.overhead_pct),
            "overhead_amount": float((mat + labor) * Decimal(str(row.overhead_pct))),
            "total_cogs": float(total_cogs),
            "part_count": row.part_count,
            "line_items": row.line_items or [],
        }

    async def build_variance(self, kit_id: str) -> dict:
        """Compare standard BOM vs actual consumed quantities."""
        result = await self.db.execute(
            text("""
                SELECT
                  ki.asset_id,
                  a.sku, a.name, a.unit_cost,
                  ki.std_qty, ki.consumed_qty,
                  ki.variance_qty,
                  ki.variance_qty * a.unit_cost AS variance_cost
                FROM kit_issues ki
                JOIN assets a ON a.id = ki.asset_id
                WHERE ki.kit_id = :kid AND ABS(ki.variance_qty) > 0.001
                ORDER BY ABS(ki.variance_qty * a.unit_cost) DESC
            """),
            {"kid": kit_id},
        )
        rows = result.fetchall()
        items = []
        total_variance_cost = Decimal("0")

        for r in rows:
            items.append({
                "asset_id": str(r.asset_id),
                "sku": r.sku, "name": r.name,
                "std_qty": float(r.std_qty),
                "consumed_qty": float(r.consumed_qty),
                "variance_qty": float(r.variance_qty),
                "variance_type": "OVER" if r.variance_qty > 0 else "UNDER",
                "variance_cost": float(r.variance_cost),
            })
            total_variance_cost += Decimal(str(r.variance_cost))

        return {
            "kit_id": kit_id,
            "variance_items": items,
            "total_variance_cost": float(total_variance_cost),
            "has_discrepancies": len(items) > 0,
        }


# ══════════════════════════════════════════════════════════════
# REORDER POINT ENGINE
# ══════════════════════════════════════════════════════════════

class ReorderEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def calculate_rop(self, asset_id: str) -> dict:
        """(Avg Daily Usage × Lead Time) + Safety Stock"""
        result = await self.db.execute(
            text("SELECT calc_rop(:id) AS rop"),
            {"id": asset_id},
        )
        rop = result.scalar() or 0

        asset = await self.db.execute(
            text("""
                SELECT qty_on_hand, qty_wip, reorder_qty,
                       safety_stock, lead_time_days, name
                FROM assets WHERE id = :id
            """),
            {"id": asset_id},
        )
        a = asset.fetchone()
        if not a:
            return {}

        qty_available = float(a.qty_on_hand) - float(a.qty_wip)
        return {
            "asset_id": asset_id,
            "name": a.name,
            "calculated_rop": float(rop),
            "qty_available": qty_available,
            "below_rop": qty_available <= float(rop),
            "suggested_order_qty": float(a.reorder_qty or 0),
            "safety_stock": float(a.safety_stock),
            "lead_time_days": float(a.lead_time_days or 0),
        }

    async def items_below_rop(self) -> list:
        """Return all items currently below their ROP."""
        result = await self.db.execute(
            text("""
                SELECT
                  a.id, a.sku, a.name,
                  a.qty_on_hand,
                  (a.qty_on_hand - a.qty_wip) AS qty_available,
                  a.reorder_point,
                  a.reorder_qty,
                  l.code AS location,
                  v.name AS vendor,
                  a.lead_time_days
                FROM assets a
                LEFT JOIN vendors v ON v.id = a.vendor_id
                LEFT JOIN locations l ON l.id = a.location_id
                WHERE a.status = 'ACTIVE'
                  AND a.reorder_point IS NOT NULL
                  AND (a.qty_on_hand - a.qty_wip) <= a.reorder_point
                ORDER BY ((a.qty_on_hand - a.qty_wip) - a.reorder_point) ASC
            """)
        )
        return [dict(r._mapping) for r in result.fetchall()]


# ══════════════════════════════════════════════════════════════
# MASS PRICE UPDATER (Admin Only)
# ══════════════════════════════════════════════════════════════

class MassPriceUpdater:
    def __init__(self, db: AsyncSession, audit: AuditService):
        self.db = db
        self.audit = audit

    async def apply_percentage(
        self,
        pct: float,
        category_id: Optional[str] = None,
        vendor_id: Optional[str] = None,
    ) -> int:
        """Apply percentage increase/decrease to a group of assets."""
        filters = "WHERE status = 'ACTIVE'"
        params: dict = {"pct": 1 + pct / 100}

        if category_id:
            filters += " AND category_id = :cat"
            params["cat"] = category_id
        if vendor_id:
            filters += " AND vendor_id = :ven"
            params["ven"] = vendor_id

        # Fetch before for audit
        before = await self.db.execute(
            text(f"SELECT id, unit_cost FROM assets {filters}"), params
        )
        rows = before.fetchall()

        await self.db.execute(
            text(f"UPDATE assets SET unit_cost = unit_cost * :pct {filters}"),
            params,
        )

        for row in rows:
            new_cost = float(row.unit_cost) * (1 + pct / 100)
            await self.audit.snapshot(
                "assets", str(row.id), "UPDATE",
                field_name="unit_cost",
                old_value=float(row.unit_cost),
                new_value=round(new_cost, 4),
            )

        return len(rows)

    async def apply_flat_rate(
        self,
        amount: float,
        category_id: Optional[str] = None,
        vendor_id: Optional[str] = None,
    ) -> int:
        """Apply flat-rate increase to asset unit cost."""
        filters = "WHERE status = 'ACTIVE'"
        params: dict = {"amt": amount}

        if category_id:
            filters += " AND category_id = :cat"
            params["cat"] = category_id
        if vendor_id:
            filters += " AND vendor_id = :ven"
            params["ven"] = vendor_id

        before = await self.db.execute(
            text(f"SELECT id, unit_cost FROM assets {filters}"), params
        )
        rows = before.fetchall()

        await self.db.execute(
            text(f"UPDATE assets SET unit_cost = GREATEST(0, unit_cost + :amt) {filters}"),
            params,
        )

        for row in rows:
            new_cost = max(0.0, float(row.unit_cost) + amount)
            await self.audit.snapshot(
                "assets", str(row.id), "UPDATE",
                field_name="unit_cost",
                old_value=float(row.unit_cost),
                new_value=round(new_cost, 4),
            )

        return len(rows)


# ══════════════════════════════════════════════════════════════
# VENDOR SCORECARD SERVICE
# ══════════════════════════════════════════════════════════════

class VendorScorecardService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_scorecard(self, vendor_id: str) -> dict:
        result = await self.db.execute(
            text("""
                SELECT
                  v.name, v.avg_lead_time_days, v.lead_time_variance,
                  v.on_time_delivery_pct, v.quality_score, v.last_scored_at,
                  COUNT(vm.id) AS total_orders,
                  AVG(vm.lead_time_delta) AS avg_lead_delta,
                  SUM(vm.qty_ncm) AS total_defects,
                  SUM(vm.qty_received) AS total_received
                FROM vendors v
                LEFT JOIN vendor_metrics vm ON vm.vendor_id = v.id
                WHERE v.id = :vid
                GROUP BY v.id
            """),
            {"vid": vendor_id},
        )
        row = result.fetchone()
        if not row:
            return {}

        defect_rate = 0.0
        if row.total_received:
            defect_rate = float(row.total_defects or 0) / float(row.total_received) * 100

        score = (
            float(row.on_time_delivery_pct or 0) * 0.4
            + (100 - defect_rate) * 0.4
            + max(0, 100 - abs(float(row.avg_lead_delta or 0)) * 5) * 0.2
        )

        return {
            "vendor_name": row.name,
            "total_orders": row.total_orders,
            "on_time_pct": float(row.on_time_delivery_pct or 0),
            "avg_lead_time_days": float(row.avg_lead_time_days or 0),
            "lead_time_variance": float(row.lead_time_variance or 0),
            "avg_lead_delta": float(row.avg_lead_delta or 0),
            "defect_rate_pct": defect_rate,
            "quality_score": float(row.quality_score or 0),
            "composite_score": round(score, 1),
            "alert": score < 70,
            "last_scored_at": row.last_scored_at.isoformat() if row.last_scored_at else None,
        }

    async def refresh_scorecard(self, vendor_id: str):
        await self.db.execute(
            text("SELECT update_vendor_scorecard(:vid)"),
            {"vid": vendor_id},
        )
