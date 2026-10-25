"""Customer endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Customer, Order
from ..services.orders import order_total, orders_for_customer

router = APIRouter(prefix="/customers")


@router.get("/report")
def customer_report(db: Session = Depends(get_db)) -> list[dict]:
    customers = db.query(Customer).order_by(Customer.id).limit(1000).all()
    report = []
    for customer in customers:
        orders = orders_for_customer(db, customer.id)  # DB-001 (indirect): one query per customer
        report.append({"customer": customer.name, "total": order_total(orders)})
    return report


@router.get("/order-counts")
def order_counts(db: Session = Depends(get_db)) -> list[dict]:
    customers = db.query(Customer).order_by(Customer.id).limit(1000).all()
    return [{"customer": c.name, "orders": len(c.orders)} for c in customers]  # DB-003: lazy load per customer


@router.get("/order-counts-fast")
def order_counts_fast(db: Session = Depends(get_db)) -> list[dict]:
    customers = db.query(Customer).options(selectinload(Customer.orders)).order_by(Customer.id).limit(1000).all()
    return [{"customer": c.name, "orders": len(c.orders)} for c in customers]


@router.get("/totals")
def customer_totals(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Customer.name, func.coalesce(func.sum(Order.total), 0.0))
        .outerjoin(Order, Order.customer_id == Customer.id)
        .group_by(Customer.id)
        .limit(1000)
        .all()
    )
    return [{"customer": name, "total": total} for name, total in rows]
