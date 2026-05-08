from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.cart.services import add_item, clear_cart, remove_item, set_item_quantity, sync_cart_for_checkout
from app.catalog.models import Product, ProductVariant


@pytest.mark.asyncio
async def test_cart_service_flow(fake_redis, test_db: AsyncSession):
    user_id = 2001
    
    product = Product(name="Test Phone", is_active=True)
    test_db.add(product)
    await test_db.flush()
    
    variant = ProductVariant(
        product_id=product.id,
        price=Decimal("999.00"),
        stock=10,
        warehouse_code="YEG-1",
        attributes={"color": "black"},
    )
    test_db.add(variant)
    await test_db.commit()
    await test_db.refresh(variant)

    await add_item(fake_redis, user_id, variant.id, 2, Decimal("999.00"))
    
    res = await sync_cart_for_checkout(fake_redis, test_db, user_id)
    assert len(res.items) == 1
    assert res.items[0].quantity == 2
    assert res.items[0].added_price == Decimal("999.00")
    assert res.items[0].current_price == Decimal("999.00")
    assert res.items[0].is_price_changed is False
    assert res.items[0].is_valid is True
    assert res.total_current_price == Decimal("1998.00")

    await set_item_quantity(fake_redis, user_id, variant.id, 5, Decimal("999.00"))
    res = await sync_cart_for_checkout(fake_redis, test_db, user_id)
    assert res.items[0].quantity == 5
    assert res.total_current_price == Decimal("4995.00")

    await remove_item(fake_redis, user_id, variant.id)
    res = await sync_cart_for_checkout(fake_redis, test_db, user_id)
    assert len(res.items) == 0

    await add_item(fake_redis, user_id, variant.id, 1, Decimal("888.00"))
    res = await sync_cart_for_checkout(fake_redis, test_db, user_id)
    assert res.items[0].is_price_changed is True
    assert res.items[0].current_price == Decimal("999.00")

    await clear_cart(fake_redis, user_id)
    res = await sync_cart_for_checkout(fake_redis, test_db, user_id)
    assert len(res.items) == 0


@pytest.mark.asyncio
async def test_cart_api_endpoints(client: AsyncClient, test_db: AsyncSession):
    user_id = 3001
    product = Product(name="API Phone", is_active=True)
    test_db.add(product)
    await test_db.flush()
    variant = ProductVariant(
        product_id=product.id,
        price=Decimal("500.00"),
        stock=5,
        warehouse_code="YYC-1",
        attributes={"storage": "128G"},
    )
    test_db.add(variant)
    await test_db.commit()
    await test_db.refresh(variant)

    res = await client.post(
        f"/api/v1/cart/items?user_id={user_id}&variant_id={variant.id}&delta_qty=2&current_price=500.00"
    )
    assert res.status_code == 204

    res = await client.get(f"/api/v1/cart/checkout-sync?user_id={user_id}")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["quantity"] == 2
    assert data["total_current_price"] == "1000.00"

    res = await client.delete(f"/api/v1/cart/items/{variant.id}?user_id={user_id}")
    assert res.status_code == 204

    res = await client.get(f"/api/v1/cart/checkout-sync?user_id={user_id}")
    assert res.status_code == 200
    assert len(res.json()["items"]) == 0
