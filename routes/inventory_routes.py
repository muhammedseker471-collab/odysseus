"""Owner-scoped stock, orders, sales reporting and marketplace CSV import."""

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, Index, func
from sqlalchemy.orm import relationship

from core.database import Base, engine, get_db_session, utcnow_naive
from src.auth_helpers import require_user


class StockProduct(Base):
    __tablename__ = "inventory_products"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=True, index=True)
    sku = Column(String, nullable=False)
    name = Column(String, nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    reorder_level = Column(Integer, nullable=False, default=5)
    price = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)
    __table_args__ = (Index("ix_inventory_products_owner_sku", "owner", "sku", unique=True),)


class StockOrder(Base):
    __tablename__ = "inventory_orders"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=True, index=True)
    external_id = Column(String, nullable=False)
    channel = Column(String, nullable=False, default="Manual")
    sku = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="paid")
    ordered_at = Column(DateTime, nullable=False, default=utcnow_naive)
    __table_args__ = (Index("ix_inventory_orders_owner_external", "owner", "channel", "external_id", unique=True),)


# Additive tables; safe for existing deployments and idempotent on restart.
Base.metadata.create_all(bind=engine, tables=[StockProduct.__table__, StockOrder.__table__])

router = APIRouter(prefix="/api/inventory", tags=["inventory"])
CHANNELS = [
    "Trendyol", "Hepsiburada", "Amazon TR", "n11", "Çiçeksepeti",
    "Pazarama", "Etsy", "eBay", "Shopify", "WooCommerce",
]
ACTIVE_STATUSES = {"paid", "processing", "shipped", "delivered"}


def _scoped(query, model, owner):
    # Never treat a missing owner as an unscoped query in multi-user mode.
    return query.filter(model.owner == owner) if owner else query.filter(model.owner.is_(None))


def _user(request):
    return require_user(request)


def _product_dict(p):
    return {"id": p.id, "sku": p.sku, "name": p.name, "stock": p.stock,
            "reorder_level": p.reorder_level, "price": p.price}


def _order_dict(o):
    return {"id": o.id, "external_id": o.external_id, "channel": o.channel,
            "sku": o.sku, "product_name": o.product_name, "quantity": o.quantity,
            "status": o.status, "ordered_at": o.ordered_at.isoformat()}


@router.get("/channels")
def channels(request: Request):
    _user(request)
    # API credentials and official marketplace approvals are configured per seller;
    # these connectors are intentionally labelled honestly until implemented.
    return [{"name": name, "status": "csv_or_manual", "connected": False} for name in CHANNELS]


@router.get("/products")
def products(request: Request, q: str = ""):
    owner = _user(request)
    with get_db_session() as db:
        query = _scoped(db.query(StockProduct), StockProduct, owner)
        if q.strip():
            needle = f"%{q.strip()[:80]}%"
            query = query.filter((StockProduct.sku.ilike(needle)) | (StockProduct.name.ilike(needle)))
        rows = query.order_by(StockProduct.name).limit(500).all()
        return [_product_dict(p) for p in rows]


@router.post("/products")
async def add_product(request: Request):
    owner = _user(request)
    try:
        body = await request.json()
        sku = str(body.get("sku", "")).strip()[:80]
        name = str(body.get("name", "")).strip()[:160]
        stock = int(body.get("stock", 0))
        reorder = int(body.get("reorder_level", 5))
        price = float(body.get("price", 0))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Ürün bilgileri geçersiz.")
    if not sku or not name or stock < 0 or reorder < 0 or price < 0:
        raise HTTPException(400, "SKU ve ürün adı gerekli; miktar ve fiyat negatif olamaz.")
    product = StockProduct(id=uuid.uuid4().hex, owner=owner or None, sku=sku,
                           name=name, stock=stock, reorder_level=reorder, price=price)
    try:
        with get_db_session() as db:
            db.add(product)
            db.flush()
            result = _product_dict(product)
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(409, "Bu SKU zaten kayıtlı.")
        raise
    return result


@router.patch("/products/{product_id}")
async def update_product(product_id: str, request: Request):
    owner = _user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Geçersiz JSON.")
    with get_db_session() as db:
        p = _scoped(db.query(StockProduct), StockProduct, owner).filter(StockProduct.id == product_id).first()
        if not p:
            raise HTTPException(404, "Ürün bulunamadı.")
        try:
            if "name" in body: p.name = str(body["name"]).strip()[:160]
            if "sku" in body: p.sku = str(body["sku"]).strip()[:80]
            if "stock" in body: p.stock = int(body["stock"])
            if "reorder_level" in body: p.reorder_level = int(body["reorder_level"])
            if "price" in body: p.price = float(body["price"])
        except (ValueError, TypeError):
            raise HTTPException(400, "Ürün bilgileri geçersiz.")
        if not p.name or not p.sku or p.stock < 0 or p.reorder_level < 0 or p.price < 0:
            raise HTTPException(400, "Ürün bilgileri geçersiz; miktar ve fiyat negatif olamaz.")
        db.flush()
        return _product_dict(p)


@router.get("/orders")
def orders(request: Request, limit: int = 100):
    owner = _user(request)
    with get_db_session() as db:
        rows = _scoped(db.query(StockOrder), StockOrder, owner).order_by(
            StockOrder.ordered_at.desc()).limit(max(1, min(limit, 500))).all()
        return [_order_dict(o) for o in rows]


def _record_order(db, owner, external_id, channel, sku, quantity, status, ordered_at=None):
    if channel not in CHANNELS:
        channel = "Diğer"
    if status not in {"pending", "paid", "processing", "shipped", "delivered", "cancelled"}:
        status = "paid"
    duplicate = _scoped(db.query(StockOrder), StockOrder, owner).filter(
        StockOrder.channel == channel, StockOrder.external_id == external_id).first()
    if duplicate:
        return False
    p = _scoped(db.query(StockProduct), StockProduct, owner).filter(StockProduct.sku == sku).first()
    if not p:
        raise HTTPException(400, f"Ürün SKU bulunamadı: {sku}")
    if quantity <= 0 or quantity > 100000:
        raise HTTPException(400, "Sipariş adedi 1–100000 arasında olmalı.")
    if status in ACTIVE_STATUSES:
        if p.stock < quantity:
            raise HTTPException(409, f"{sku} stok yetersiz: mevcut {p.stock}, sipariş {quantity}.")
        p.stock -= quantity
    db.add(StockOrder(id=uuid.uuid4().hex, owner=owner or None,
                      external_id=external_id[:120], channel=channel, sku=sku,
                      product_name=p.name, quantity=quantity, status=status,
                      ordered_at=ordered_at or utcnow_naive()))
    return True


@router.post("/orders")
async def add_order(request: Request):
    owner = _user(request)
    try:
        body = await request.json()
        external_id = str(body.get("external_id") or uuid.uuid4().hex).strip()
        channel = str(body.get("channel") or "Manual").strip()
        sku = str(body.get("sku") or "").strip()
        quantity = int(body.get("quantity", 0))
        status = str(body.get("status") or "paid").strip().lower()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Sipariş bilgileri geçersiz.")
    if not sku or not external_id:
        raise HTTPException(400, "SKU gerekli.")
    try:
        with get_db_session() as db:
            created = _record_order(db, owner, external_id, channel, sku, quantity, status)
    except HTTPException:
        raise
    except Exception as exc:
        if "unique" in str(exc).lower():
            return {"ok": True, "duplicate": True}
        raise
    return {"ok": True, "duplicate": not created}


@router.post("/orders/import")
async def import_orders(request: Request):
    """Import generic marketplace CSV: external_order_id,channel,sku,quantity,status."""
    owner = _user(request)
    content_type = request.headers.get("content-type", "")
    if "text/csv" not in content_type and "application/octet-stream" not in content_type:
        raise HTTPException(415, "CSV dosyasını text/csv olarak gönderin.")
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "CSV en fazla 2 MB olabilir.")
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or not {"sku", "quantity"}.issubset({x.strip().lower() for x in reader.fieldnames}):
            raise ValueError("CSV başlıklarında sku ve quantity olmalı.")
        normalized = {x.strip().lower(): x for x in reader.fieldnames}
        created = duplicates = 0
        with get_db_session() as db:
            for i, row in enumerate(reader):
                if i >= 5000: raise HTTPException(413, "Tek seferde en fazla 5000 sipariş içe aktarılabilir.")
                get = lambda key, default="": (row.get(normalized.get(key, ""), default) or default).strip()
                quantity = int(get("quantity", "0"))
                external = get("external_order_id") or get("order_id") or f"csv-{uuid.uuid4().hex}"
                added = _record_order(db, owner, external, get("channel", "Manual"),
                                      get("sku"), quantity, get("status", "paid").lower())
                created += int(added); duplicates += int(not added)
        return {"ok": True, "imported": created, "duplicates": duplicates}
    except HTTPException:
        raise
    except (UnicodeDecodeError, csv.Error, ValueError, TypeError) as exc:
        raise HTTPException(400, f"CSV okunamadı: {exc}")


@router.get("/summary")
def summary(request: Request, days: int = 7):
    owner = _user(request)
    days = max(1, min(days, 90))
    cutoff = utcnow_naive() - timedelta(days=days)
    with get_db_session() as db:
        products_q = _scoped(db.query(StockProduct), StockProduct, owner)
        products = products_q.all()
        orders_q = _scoped(db.query(StockOrder), StockOrder, owner).filter(
            StockOrder.ordered_at >= cutoff, StockOrder.status.in_(ACTIVE_STATUSES))
        sold = orders_q.with_entities(func.coalesce(func.sum(StockOrder.quantity), 0)).scalar() or 0
        low = [p for p in products if p.stock <= p.reorder_level]
        daily = orders_q.with_entities(func.date(StockOrder.ordered_at), func.sum(StockOrder.quantity)).group_by(
            func.date(StockOrder.ordered_at)).all()
        return {"product_count": len(products), "total_stock": sum(p.stock for p in products),
                "low_stock_count": len(low), "low_stock": [_product_dict(p) for p in low],
                "sold_in_period": int(sold), "period_days": days,
                "daily_sales": [{"date": str(d), "quantity": int(q)} for d, q in daily]}
