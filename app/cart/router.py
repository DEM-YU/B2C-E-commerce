from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from app.core.redis import AsyncRedis as Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cart import services
from app.cart.schemas import CartCheckoutResponse
from app.core.database import get_db
from app.core.redis import get_redis

router = APIRouter(prefix="/cart", tags=["Cart"])


@router.post("/items", status_code=204, response_class=Response, response_model=None)
async def add_cart_item(
    user_id: int = Query(...),
    variant_id: int = Query(...),
    delta_qty: int = Query(..., gt=0, le=99),
    current_price: Decimal = Query(..., gt=0),
    redis: Redis = Depends(get_redis),
) -> None:
    await services.add_item(redis, user_id, variant_id, delta_qty, current_price)


@router.delete("/items/{variant_id}", status_code=204, response_class=Response, response_model=None)
async def remove_cart_item(
    variant_id: int,
    user_id: int = Query(...),
    redis: Redis = Depends(get_redis),
) -> None:
    await services.remove_item(redis, user_id, variant_id)


@router.delete("", status_code=204, response_class=Response, response_model=None)
async def clear_cart(
    user_id: int = Query(...),
    redis: Redis = Depends(get_redis),
) -> None:
    await services.clear_cart(redis, user_id)


@router.get("/checkout-sync", response_model=CartCheckoutResponse)
async def checkout_sync(
    user_id: int = Query(...),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> CartCheckoutResponse:
    return await services.sync_cart_for_checkout(redis, db, user_id)

