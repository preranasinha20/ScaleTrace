"""Orders demo application -- FIXED (reference) variant.

Same API and response as ../orders_app_defective, but ``list_order_summaries`` aggregates
in one query instead of querying once per user. ScaleTrace should report no DB-001 here.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Order, User


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Orders demo (fixed)", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/seed")
def seed(users: int = 100, orders_per_user: int = 5, db: Session = Depends(get_db)) -> dict[str, int]:
    """Reset the demo data to a chosen size, so workloads can vary the input size."""
    db.query(Order).delete()
    db.query(User).delete()
    new_users = [User(id=i + 1, name=f"user-{i + 1}") for i in range(users)]
    new_orders = [
        Order(user_id=user.id, total=float(10 + n))
        for user in new_users
        for n in range(orders_per_user)
    ]
    db.add_all(new_users)
    db.add_all(new_orders)
    db.commit()
    return {"users": users, "orders": len(new_orders)}


@app.get("/orders/summary")
def list_order_summaries(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(
            User.id,
            User.name,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total), 0.0),
        )
        .outerjoin(Order, Order.user_id == User.id)
        .group_by(User.id, User.name)
        .order_by(User.id)
        .all()
    )
    return [
        {"user_id": user_id, "name": name, "order_count": count, "total": total}
        for user_id, name, count, total in rows
    ]
