"""Owner-scoped stock, orders, sales reporting and marketplace CSV import."""

import csv
import io
import math
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import Column, DateTime, Float, Integer, String, Index, func

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


Base.metadata.create_all(bind=engine, tables=[StockProduct.__table__, StockOrder.__table__])
router = APIRouter(prefix="/api/inventory", tags=["inventory"])
CHANNELS = ["Trendyol", "Hepsiburada", "Amazon TR", "n11", "Çiçeksepeti", "Pazarama", "Etsy", "eBay", "Shopify", "WooCommerce"]
ACTIVE_STATUSES = {"paid", "processing", "shipped", "delivered"}
VALID_STATUSES = ACTIVE_STATUSES | {"pending", "cancelled"}


def _scoped(query, model, owner):
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
    return [{"name": name, "status": "csv_or_manual", "connected": False} for name in CHANNELS]


@router.get("/products")
def products(request: Request, q: str = ""):
    owner = _user(request)
    with get_db_session() as db:
        query = _scoped(db.query(StockProduct), StockProduct, owner)
        if q.strip():
            needle = f"%{q.strip()[:80]}%"
            query = query.filter((StockProduct.sku.ilike(needle)) | (StockProduct.name.ilike(needle)))
        return [_product_dict(p) for p in query.order_by(StockProduct.name).limit(500).all()]


@router.post("/products")
async def add_product(request: Request):
    owner = _user(request)
    try:
        body = await request.json()
        sku, name = str(body.get("sku", "")).strip()[:80], str(body.get("name", "")).strip()[:160]
        stock, reorder, price = int(body.get("stock", 0)), int(body.get("reorder_level", 5)), float(body.get("price", 0))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Ürün bilgileri geçersiz.")
    if not sku or not name or stock < 0 or reorder < 0 or price < 0:
        raise HTTPException(400, "SKU ve ürün adı gerekli; miktar ve fiyat negatif olamaz.")
    product = StockProduct(id=uuid.uuid4().hex, owner=owner or None, sku=sku, name=name,
                           stock=stock, reorder_level=reorder, price=price)
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
    if not isinstance(body, dict):
        raise HTTPException(400, "Ürün bilgileri geçersiz.")
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
        rows = _scoped(db.query(StockOrder), StockOrder, owner).order_by(StockOrder.ordered_at.desc()).limit(max(1, min(limit, 500))).all()
        return [_order_dict(o) for o in rows]


def _record_order(db, owner, external_id, channel, sku, quantity, status, ordered_at=None):
    channel = channel if channel in CHANNELS or channel == "Manual" else "Diğer"
    status = status if status in VALID_STATUSES else "paid"
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
    db.add(StockOrder(id=uuid.uuid4().hex, owner=owner or None, external_id=external_id[:120],
                      channel=channel, sku=sku, product_name=p.name, quantity=quantity,
                      status=status, ordered_at=ordered_at or utcnow_naive()))
    return True


@router.post("/orders")
async def add_order(request: Request):
    owner = _user(request)
    try:
        body = await request.json()
        external_id = str(body.get("external_id") or uuid.uuid4().hex).strip()
        channel, sku = str(body.get("channel") or "Manual").strip(), str(body.get("sku") or "").strip()
        quantity, status = int(body.get("quantity", 0)), str(body.get("status") or "paid").strip().lower()
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
    if "text/csv" not in request.headers.get("content-type", "") and "application/octet-stream" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "CSV dosyasını text/csv olarak gönderin.")
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "CSV en fazla 2 MB olabilir.")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not reader.fieldnames or not {"sku", "quantity"}.issubset({x.strip().lower() for x in reader.fieldnames}):
            raise ValueError("CSV başlıklarında sku ve quantity olmalı.")
        normalized = {x.strip().lower(): x for x in reader.fieldnames}
        created = duplicates = 0
        with get_db_session() as db:
            for i, row in enumerate(reader):
                if i >= 5000:
                    raise HTTPException(413, "Tek seferde en fazla 5000 sipariş içe aktarılabilir.")
                get = lambda key, default="": (row.get(normalized.get(key, ""), default) or default).strip()
                quantity = int(get("quantity", "0"))
                external = get("external_order_id") or get("order_id") or f"csv-{uuid.uuid4().hex}"
                added = _record_order(db, owner, external, get("channel", "Manual"), get("sku"), quantity, get("status", "paid").lower())
                created += int(added)
                duplicates += int(not added)
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
    analysis_cutoff = utcnow_naive() - timedelta(days=30)
    with get_db_session() as db:
        all_products = _scoped(db.query(StockProduct), StockProduct, owner).order_by(StockProduct.name).all()
        period_orders = _scoped(db.query(StockOrder), StockOrder, owner).filter(
            StockOrder.ordered_at >= cutoff, StockOrder.status.in_(ACTIVE_STATUSES))
        sold = period_orders.with_entities(func.coalesce(func.sum(StockOrder.quantity), 0)).scalar() or 0
        daily = period_orders.with_entities(func.date(StockOrder.ordered_at), func.sum(StockOrder.quantity)).group_by(func.date(StockOrder.ordered_at)).all()
        channel_rows = period_orders.with_entities(StockOrder.channel, func.sum(StockOrder.quantity)).group_by(StockOrder.channel).all()
        sku_rows = _scoped(db.query(StockOrder), StockOrder, owner).filter(
            StockOrder.ordered_at >= analysis_cutoff, StockOrder.status.in_(ACTIVE_STATUSES)
        ).with_entities(StockOrder.sku, func.sum(StockOrder.quantity)).group_by(StockOrder.sku).all()
        sales_by_sku = {sku: int(quantity or 0) for sku, quantity in sku_rows}
        low, suggestions = [], []
        for p in all_products:
            recent = sales_by_sku.get(p.sku, 0)
            daily_rate = recent / 30
            days_left = round(p.stock / daily_rate, 1) if daily_rate > 0 else None
            is_low = p.stock <= p.reorder_level
            if is_low:
                low.append(_product_dict(p))
            if is_low or (days_left is not None and days_left <= 7):
                # Suggest 14 days of forecast sales, while respecting the configured threshold.
                target = max(p.reorder_level * 2, math.ceil(daily_rate * 14))
                suggestions.append({**_product_dict(p), "sold_last_30_days": recent,
                                    "estimated_days_left": days_left,
                                    "suggested_reorder": max(0, target - p.stock),
                                    "reason": "critical_stock" if is_low else "fast_selling"})
        return {"product_count": len(all_products), "total_stock": sum(p.stock for p in all_products),
                "low_stock_count": len(low), "low_stock": low,
                "sold_in_period": int(sold), "period_days": days,
                "daily_sales": [{"date": str(d), "quantity": int(q)} for d, q in daily],
                "channel_sales": [{"channel": str(c), "quantity": int(q)} for c, q in channel_rows],
                "reorder_suggestions": suggestions}
