"""Order helpers used by the routers."""

from sqlalchemy.orm import Session

from ..models import Order


def orders_for_customer(db: Session, customer_id: int) -> list[Order]:
    return db.query(Order).filter(Order.customer_id == customer_id).limit(500).all()


def order_total(orders: list[Order]) -> float:
    return sum(order.total for order in orders)
