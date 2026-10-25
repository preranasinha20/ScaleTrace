"""Shop demo application with several injected scalability risks (see scaletrace_groundtruth.json)."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import Customer, Order, Product
from .routers import catalog, customers


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Shop demo", lifespan=lifespan)
app.include_router(customers.router)
app.include_router(catalog.router)


@app.post("/seed")
def seed(size: int = 100, db: Session = Depends(get_db)) -> dict[str, int]:
    """Reset demo data: `size` customers with 3 orders each, and `size` products."""
    db.query(Order).delete()
    db.query(Customer).delete()
    db.query(Product).delete()
    new_customers = [Customer(id=i + 1, name=f"customer-{i + 1}") for i in range(size)]
    db.add_all(new_customers)
    db.add_all([Order(customer_id=c.id, total=float(n + 1)) for c in new_customers for n in range(3)])
    db.add_all([Product(id=i + 1, sku=f"SKU-{i + 1:05d}", title=f"Product number {i + 1}") for i in range(size)])
    db.commit()
    return {"customers": size, "products": size}
