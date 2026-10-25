"""Orders demo application -- DEFECTIVE variant.

Contains one intentional scalability-risk pattern, labelled in scaletrace_groundtruth.json:

* DB-001 in ``list_order_summaries``: one ``orders`` query per user inside a loop, so the
  number of queries per request grows with the number of users (1 + N).

The fixed variant in ../orders_app_fixed computes the same response with a single query.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Order, User


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Orders demo (defective)", lifespan=lifespan)


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
    users = db.query(User).order_by(User.id).all()
    summaries = []
    for user in users:
        orders = db.query(Order).filter(Order.user_id == user.id).all()  # DB-001: one query per user
        summaries.append(
            {
                "user_id": user.id,
                "name": user.name,
                "order_count": len(orders),
                "total": sum(order.total for order in orders),
            }
        )
    return summaries
