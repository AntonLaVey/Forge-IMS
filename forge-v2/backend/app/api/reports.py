"""Budget and reporting endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.services.inventory import ReorderEngine, VendorScorecardService

budget_router = APIRouter(prefix="/budget")


@budget_router.get("/dashboard")
async def budget_dashboard(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    spend = await db.execute(
        text(
            """
            SELECT budget_category, SUM(total) AS total_spend, COUNT(*) AS po_count
            FROM purchase_orders
            WHERE status NOT IN ('DRAFT','CANCELLED')
              AND DATE_TRUNC('month', COALESCE(ordered_date, created_at::date)) = DATE_TRUNC('month', CURRENT_DATE)
            GROUP BY budget_category
            """
        )
    )
    budgets = await db.execute(
        text(
            """
            SELECT category, SUM(amount) AS budget
            FROM budgets
            WHERE fiscal_year = EXTRACT(YEAR FROM CURRENT_DATE)
              AND fiscal_month = EXTRACT(MONTH FROM CURRENT_DATE)
            GROUP BY category
            """
        )
    )
    return {
        "monthly_spend": [dict(r._mapping) for r in spend.fetchall()],
        "budgets": [dict(r._mapping) for r in budgets.fetchall()],
    }


router = APIRouter(prefix="/reports")


@router.get("/reorder-alerts")
async def reorder_alerts(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    engine = ReorderEngine(db)
    return await engine.items_below_rop()


@router.get("/ncm-summary")
async def ncm_summary(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text(
            """
            SELECT n.reason, COUNT(*) AS count, SUM(n.total_loss) AS total_loss
            FROM ncm_logs n GROUP BY n.reason ORDER BY total_loss DESC
            """
        )
    )
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/vendor-scorecard/{vendor_id}")
async def vendor_scorecard(vendor_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    svc = VendorScorecardService(db)
    return await svc.get_scorecard(vendor_id)


@router.get("/inventory-value")
async def inventory_value(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        text(
            """
            SELECT c.name AS category,
                   SUM(a.qty_on_hand * a.unit_cost) AS total_value,
                   COUNT(a.id) AS item_count,
                   SUM(a.qty_ncm * a.unit_cost) AS ncm_value
            FROM assets a JOIN categories c ON c.id = a.category_id
            WHERE a.status = 'ACTIVE'
            GROUP BY c.name ORDER BY total_value DESC
            """
        )
    )
    return [dict(r._mapping) for r in result.fetchall()]
