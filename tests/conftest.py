"""
tests/conftest.py
Test fixtures: test postgres db, fakeredis, and fastapi dependency overrides.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.catalog.models import Product, ProductVariant
from app.core.base import Base
from app.core.database import get_db
from app.core.redis import get_redis
from app.transaction.models import Order, OrderItem, OrderStatus
from main import app

# 1. test db url (postgres instead of sqlite for jsonb and fk support)

TEST_DATABASE_URL = (
    "postgresql+asyncpg://admin:secret@localhost:5433/ecommerce_test_db"
)

# 2. function-scoped async engine fixture
@pytest_asyncio.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """create a new engine for each test case to keep asyncpg event loop clean."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
    )

    # create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    yield engine

    # clean up tables after test
    async with engine.begin() as conn:
        await conn.execute(text(
            'TRUNCATE TABLE order_item, "order", product_variant, product '
            "RESTART IDENTITY CASCADE"
        ))

    await engine.dispose()


# 3. function-scoped db session fixture

@pytest_asyncio.fixture
async def test_db(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """get an async session connected to the test db."""
    session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# 4. fakeredis fixture

@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    """get a fresh fakeredis instance for each test."""
    redis = FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


# 5. async client fixture with overridden db and redis dependencies

@pytest_asyncio.fixture
async def client(
    test_db: AsyncSession,
    fake_redis: FakeRedis,
) -> AsyncGenerator[AsyncClient, None]:
    """inject test db and fakeredis into fastapi app dependencies."""
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield test_db

    async def _override_get_redis() -> FakeRedis:
        return fake_redis

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# 6. setup test products and variants

@pytest_asyncio.fixture
async def setup_test_data(test_db: AsyncSession) -> dict:
    """create test variants with prices 33.33 and 66.67 (sum = 100.00)."""
    product = Product(name="Test Product - Financial Grade", is_active=True)
    test_db.add(product)
    await test_db.flush()

    variant_yeg = ProductVariant(
        product_id=product.id,
        price=Decimal("33.33"),
        stock=100,
        warehouse_code="YEG-1",
        attributes={},
    )
    variant_yyc = ProductVariant(
        product_id=product.id,
        price=Decimal("66.67"),
        stock=50,
        warehouse_code="YYC-2",
        attributes={},
    )
    test_db.add_all([variant_yeg, variant_yyc])
    await test_db.flush()
    await test_db.commit()

    await test_db.refresh(variant_yeg)
    await test_db.refresh(variant_yyc)

    return {
        "product_id": product.id,
        "variant_yeg": variant_yeg,
        "variant_yyc": variant_yyc,
    }


# 7. helper: create paid order directly in db

async def create_paid_order_in_db(
    db: AsyncSession,
    user_id: int,
    variant: ProductVariant,
    quantity: int = 1,
) -> tuple[Order, OrderItem]:
    """create order directly in db without calling http endpoints."""
    unit_price = variant.price
    actual = (unit_price * Decimal(str(quantity))).quantize(Decimal("0.01"))

    order = Order(
        user_id=user_id,
        status=OrderStatus.PAID,
        total_amount=actual,
        idempotency_key=str(uuid.uuid4()),
        parent_id=None,
    )
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id,
        variant_id=variant.id,
        unit_price=unit_price,
        quantity=quantity,
        prorated_discount=Decimal("0.00"),
        actual_paid_price=actual,
        refunded_quantity=0,
        refunded_amount=Decimal("0.00"),
        is_fully_refunded=False,
    )
    db.add(item)
    await db.commit()
    await db.refresh(order)
    await db.refresh(item)

    return order, item
