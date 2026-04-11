from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from app.core.redis import AsyncRedis as Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload

from app.catalog.models import Product, ProductVariant
from app.cart.schemas import CartCheckoutResponse, CartItemResponse

_CART_KEY_TPL = "cart:{user_id}"
CART_TTL = 604_800


def _cart_key(user_id: int) -> str:
    return _CART_KEY_TPL.format(user_id=user_id)


def _encode_item(quantity: int, price: Decimal) -> str:
    return json.dumps({"qty": quantity, "price": str(price)})


def _decode_item(raw: str) -> dict[str, Any]:
    return json.loads(raw)


_LUA_HSET_ADD_QTY = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
local old_qty = 0
if raw then
    local ok, data = pcall(cjson.decode, raw)
    if ok and data and data.qty then
        old_qty = tonumber(data.qty)
    end
end
local new_qty = old_qty + tonumber(ARGV[2])
if new_qty <= 0 then
    redis.call('HDEL', KEYS[1], ARGV[1])
else
    local new_val = cjson.encode({qty=new_qty, price=ARGV[3]})
    redis.call('HSET', KEYS[1], ARGV[1], new_val)
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return new_qty
"""


async def add_item(
    redis: Redis,
    user_id: int,
    variant_id: int,
    delta_qty: int,
    current_price: Decimal,
) -> None:
    key = _cart_key(user_id)
    try:
        await redis.eval(
            _LUA_HSET_ADD_QTY,
            1,
            key,
            str(variant_id),
            str(delta_qty),
            str(current_price),
            str(CART_TTL),
        )
    except Exception:
        raw = await redis.hget(key, str(variant_id))
        old_qty = 0
        if raw:
            try:
                data = _decode_item(raw if isinstance(raw, str) else raw.decode())
                old_qty = int(data.get("qty", 0))
            except Exception:
                old_qty = 0
        new_qty = old_qty + delta_qty
        if new_qty <= 0:
            await redis.hdel(key, str(variant_id))
        else:
            val = _encode_item(new_qty, current_price)
            await redis.hset(key, str(variant_id), val)
        await redis.expire(key, CART_TTL)


async def set_item_quantity(
    redis: Redis,
    user_id: int,
    variant_id: int,
    quantity: int,
    current_price: Decimal,
) -> None:
    key = _cart_key(user_id)
    if quantity <= 0:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hdel(key, str(variant_id))
            pipe.expire(key, CART_TTL)
            await pipe.execute()
        return
    value = _encode_item(quantity, current_price)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, str(variant_id), value)
        pipe.expire(key, CART_TTL)
        await pipe.execute()


async def remove_item(
    redis: Redis,
    user_id: int,
    variant_id: int,
) -> None:
    key = _cart_key(user_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hdel(key, str(variant_id))
        pipe.expire(key, CART_TTL)
        await pipe.execute()


async def clear_cart(
    redis: Redis,
    user_id: int,
) -> None:
    await redis.delete(_cart_key(user_id))


async def sync_cart_for_checkout(
    redis: Redis,
    db: AsyncSession,
    user_id: int,
) -> CartCheckoutResponse:
    key = _cart_key(user_id)
    raw_cart: dict[bytes | str, bytes | str] = await redis.hgetall(key)

    if not raw_cart:
        return CartCheckoutResponse(items=[], total_current_price=Decimal("0.00"))

    cart_snapshot: dict[int, dict[str, Any]] = {}
    for field, value in raw_cart.items():
        vid = int(field)
        data = _decode_item(value if isinstance(value, str) else value.decode())
        cart_snapshot[vid] = {
            "qty": int(data["qty"]),
            "price": Decimal(data["price"]),
        }

    variant_ids = list(cart_snapshot.keys())

    rows = await db.execute(
        select(ProductVariant)
        .options(joinedload(ProductVariant.product))
        .where(ProductVariant.id.in_(variant_ids))
    )
    variants: list[ProductVariant] = rows.scalars().all()
    variant_map: dict[int, ProductVariant] = {v.id: v for v in variants}

    items: list[CartItemResponse] = []
    total_current_price = Decimal("0.00")

    for vid, snapshot in cart_snapshot.items():
        qty: int = snapshot["qty"]
        added_price: Decimal = snapshot["price"]

        db_variant = variant_map.get(vid)

        if db_variant is None:
            items.append(
                CartItemResponse(
                    variant_id=vid,
                    quantity=qty,
                    added_price=added_price,
                    current_price=added_price,
                    is_price_changed=False,
                    is_valid=False,
                )
            )
            continue

        current_price: Decimal = db_variant.price
        is_price_changed: bool = current_price != added_price
        is_valid: bool = (
            db_variant.stock >= qty
            and db_variant.product.is_active
        )

        if is_valid:
            total_current_price += current_price * qty

        items.append(
            CartItemResponse(
                variant_id=vid,
                quantity=qty,
                added_price=added_price,
                current_price=current_price,
                is_price_changed=is_price_changed,
                is_valid=is_valid,
            )
        )

    return CartCheckoutResponse(
        items=items,
        total_current_price=total_current_price,
    )

