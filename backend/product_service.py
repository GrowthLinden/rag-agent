from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Product
from time_utils import utc_now


PRODUCT_INTENT_KEYWORDS = (
    "商品",
    "产品",
    "价格",
    "多少钱",
    "库存",
    "有货",
    "材质",
    "规格",
    "参数",
    "类别",
    "图片",
    "照片",
    "图",
)


def serialize_product(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category or "",
        "price": product.price or "",
        "stock": int(product.stock or 0),
        "material": product.material or "",
        "specifications": product.specifications or "",
        "image_url": product.image_url or "",
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


def list_products(db: Session, keyword: str | None = None) -> list[Product]:
    query = db.query(Product)
    normalized = (keyword or "").strip()
    if normalized:
        like = f"%{normalized}%"
        query = query.filter(
            or_(
                Product.name.ilike(like),
                Product.category.ilike(like),
                Product.material.ilike(like),
                Product.specifications.ilike(like),
            )
        )
    return query.order_by(Product.updated_at.desc(), Product.id.desc()).all()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _text_value(product: Product | dict[str, Any], key: str) -> str:
    if isinstance(product, dict):
        return str(product.get(key) or "")
    return str(getattr(product, key, "") or "")


def _product_search_text(product: Product | dict[str, Any]) -> str:
    return "\n".join(
        [
            _text_value(product, "name"),
            _text_value(product, "category"),
            _text_value(product, "price"),
            _text_value(product, "material"),
            _text_value(product, "specifications"),
        ]
    )


def _query_terms(query: str) -> set[str]:
    raw = (query or "").lower()
    terms = {item for item in re.findall(r"[a-z0-9]+", raw) if len(item) >= 2}
    for chinese_run in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chinese_run) >= 2:
            max_len = min(6, len(chinese_run))
            for size in range(2, max_len + 1):
                for idx in range(0, len(chinese_run) - size + 1):
                    terms.add(chinese_run[idx:idx + size])
    return terms


def product_match_score(product: Product | dict[str, Any], query: str) -> int:
    query_norm = _normalize(query)
    if not query_norm:
        return 0

    name_norm = _normalize(_text_value(product, "name"))
    text_norm = _normalize(_product_search_text(product))
    score = 0

    if name_norm and query_norm == name_norm:
        score += 180
    elif name_norm and name_norm in query_norm:
        score += 130
    elif name_norm and len(query_norm) >= 2 and query_norm in name_norm:
        score += 90

    if any(keyword in query_norm for keyword in PRODUCT_INTENT_KEYWORDS):
        score += 10

    for term in _query_terms(query):
        if name_norm and term in name_norm:
            score += 24
        elif term in text_norm:
            score += 6

    return score


def search_products_for_query(db: Session, query: str, limit: int = 3, min_score: int = 35) -> list[tuple[Product, int]]:
    products = db.query(Product).order_by(Product.updated_at.desc()).limit(1000).all()
    scored = [(product, product_match_score(product, query)) for product in products]
    scored = [(product, score) for product, score in scored if score >= min_score]
    scored.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
    return scored[:limit]


def format_product_answer(product: Product | dict[str, Any]) -> str:
    name = _text_value(product, "name")
    category = _text_value(product, "category") or "未填写"
    price = _text_value(product, "price") or "未填写"
    stock = _text_value(product, "stock") or "0"
    material = _text_value(product, "material") or "未填写"
    specifications = _text_value(product, "specifications") or "未填写"
    image_url = _text_value(product, "image_url")

    lines = [
        f"以下信息来自商家商品库：**{name}**",
        "",
        f"- 类别：{category}",
        f"- 价格：{price}",
        f"- 库存：{stock}",
        f"- 材质：{material}",
        f"- 规格：{specifications}",
    ]
    if image_url:
        lines.extend(["", f"![{name}]({image_url})"])
    return "\n".join(lines)


def search_product_answer(query: str) -> str | None:
    db = SessionLocal()
    try:
        matches = search_products_for_query(db, query, limit=1)
        if not matches:
            return None
        product, _score = matches[0]
        return format_product_answer(product)
    finally:
        db.close()


def apply_product_update(
    product: Product,
    *,
    name: str,
    category: str,
    price: str,
    stock: int,
    material: str,
    specifications: str,
    image_url: str | None = None,
) -> None:
    product.name = name
    product.category = category
    product.price = price
    product.stock = stock
    product.material = material
    product.specifications = specifications
    if image_url is not None:
        product.image_url = image_url
    product.updated_at = utc_now()
