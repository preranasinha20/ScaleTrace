"""Catalog endpoints."""

import re

import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product

router = APIRouter(prefix="/catalog")

RECENT_SEARCHES = []
PRICING_URL = "https://pricing.example.com/v1/prices"


@router.get("/search")
def search(q: str, db: Session = Depends(get_db)) -> list[str]:
    RECENT_SEARCHES.append(q)  # MEM-002: grows for the lifetime of the process
    products = db.query(Product).all()  # MEM-001: whole table loaded
    matches = []
    for product in products:
        pattern = re.compile(re.escape(q), re.IGNORECASE)  # CMP-001: recompiled per product
        if pattern.search(product.title):
            matches.append(product.sku)
    return matches


@router.get("/prices")
def prices(db: Session = Depends(get_db)) -> dict[str, float]:
    products = db.query(Product).limit(100).all()
    result = {}
    for product in products:
        response = requests.get(f"{PRICING_URL}/{product.sku}", timeout=5)  # EXT-001: one call per product
        result[product.sku] = response.json()["price"]
    return result
