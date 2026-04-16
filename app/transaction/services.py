"""
app/transaction/services.py
Order management and transaction processing services.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload, selectinload

from app.catalog.models import Product, ProductVariant
from app.transaction.models import Order, OrderItem, OrderStatus
from app.transaction.schemas import OrderCreate, RefundRequest

_REFUND_IDEM_KEY_TPL = "refund_idem:{key}"
_REFUND_IDEM_TTL = 86_400   # 24 Hours

_TWO_PLACES = Decimal("0.01")
_MIN_PAYABLE = Decimal("0.01")


def calculate_final_price(
    original_amount: Decimal,
    threshold: Decimal,
    reduction: Decimal,
    discount_rate: Decimal,
) -> Decimal:
    """
    Calculate order price after threshold reductions and discounts.
    """
    amount = original_amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)
    if amount >= threshold:
        amount = (amount - reduction).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)

    amount = (amount * discount_rate).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)

    if amount <= Decimal("0"):
        return _MIN_PAYABLE

    return amount


def prorate_order_discounts(
    items: list[dict],
    total_discount: Decimal,
) -> list[dict]:
    """
    Prorate order discount across items, adjusting the last item for rounding differences.
    """
    total_goods_amount: Decimal = sum(item["amount"] for item in items)
    if total_goods_amount == Decimal("0") or total_discount == Decimal("0"):
        for item in items:
            item["prorated_discount"] = Decimal("0.00")
            item["actual_paid_price"] = item["amount"].quantize(
                _TWO_PLACES, rounding=ROUND_HALF_EVEN
            )
        return items

    accumulated_discount = Decimal("0.00")

    for idx, item in enumerate(items):
        is_last = idx == len(items) - 1

        if is_last:
            item_discount = total_discount - accumulated_discount
        else:
            item_discount = (
                (item["amount"] / total_goods_amount) * total_discount
            ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)
            accumulated_discount += item_discount

        item["prorated_discount"] = item_discount
        item["actual_paid_price"] = (
            item["amount"] - item_discount
        ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)

    return items


class IllegalOrderStateError(Exception):
    """
    Raised on invalid order status transitions.
    """
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"Illegal state transition: {current.value} -> {target.value}"
        )


_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING:            frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID:               frozenset({OrderStatus.SHIPPED, OrderStatus.REFUNDING}),
    OrderStatus.SHIPPED:            frozenset({OrderStatus.COMPLETED}),
    OrderStatus.REFUNDING:          frozenset({OrderStatus.REFUNDED}),
    OrderStatus.PARTIALLY_REFUNDED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REFUNDED:  frozenset(),
    OrderStatus.SPLIT:     frozenset(),
}


def validate_status_transition(
    current_status: OrderStatus,
    target_status: OrderStatus,
) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_status not in allowed:
        raise IllegalOrderStateError(current_status, target_status)


async def create_order_transaction(
    db: AsyncSession,
    order_in: OrderCreate,
    idempotency_key: str,
) -> Order:
    """
    Create an order and update inventory stock in a transaction.
    """
    # 0. Check idempotency
    existing = await db.execute(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )
    existing_order: Order | None = existing.scalar_one_or_none()
    if existing_order is not None:
        raise HTTPException(
            status_code=409,
            detail="Order already created for this idempotency key",
        )

    # 1. Fetch and validate variants
    variant_ids = [item.variant_id for item in order_in.items]
    rows = await db.execute(
        select(ProductVariant)
        .options(joinedload(ProductVariant.product))
        .where(ProductVariant.id.in_(variant_ids))
    )
    variants: list[ProductVariant] = rows.scalars().all()
    variant_map: dict[int, ProductVariant] = {v.id: v for v in variants}

    missing = set(variant_ids) - variant_map.keys()
    if missing:
        raise HTTPException(
            status_code=404,
            detail="Product variant not found",
        )

    inactive = [
        vid for vid, v in variant_map.items() if not v.product.is_active
    ]
    if inactive:
        raise HTTPException(
            status_code=422,
            detail="Product is inactive",
        )

    # 2. Pre-check stock
    for item in order_in.items:
        variant = variant_map[item.variant_id]
        if variant.stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail="Insufficient stock",
            )

    # 3. Pricing & discount proration
    item_rows: list[dict] = [
        {
            "variant_id": item.variant_id,
            "quantity": item.quantity,
            "unit_price": variant_map[item.variant_id].price,
            "amount": (
                variant_map[item.variant_id].price * item.quantity
            ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN),
        }
        for item in order_in.items
    ]

    raw_total: Decimal = sum(row["amount"] for row in item_rows)
    final_amount = calculate_final_price(
        original_amount=raw_total,
        threshold=Decimal("999999999.99"),
        reduction=Decimal("0.00"),
        discount_rate=Decimal("1"),
    )

    total_discount: Decimal = (raw_total - final_amount).quantize(
        _TWO_PLACES, rounding=ROUND_HALF_EVEN
    )

    prorate_order_discounts(item_rows, total_discount)

    # 4. Save order records
    order = Order(
        user_id=order_in.user_id,
        status=OrderStatus.PENDING,
        total_amount=final_amount,
        idempotency_key=idempotency_key,
        items=[
            OrderItem(
                variant_id=row["variant_id"],
                unit_price=row["unit_price"],
                quantity=row["quantity"],
                prorated_discount=row["prorated_discount"],
                actual_paid_price=row["actual_paid_price"],
                refunded_amount=Decimal("0.00"),
            )
            for row in item_rows
        ],
    )
    db.add(order)
    await db.flush()

    # 5. Deduct inventory with row locks
    # Sort items by variant_id to avoid AB-BA deadlocks
    for item in sorted(order_in.items, key=lambda x: x.variant_id):
        deduct_result = await db.execute(
            text(
                "UPDATE product_variant "
                "SET stock = stock - :qty "
                "WHERE id = :vid AND stock >= :qty"
            ),
            {"qty": item.quantity, "vid": item.variant_id},
        )
        if deduct_result.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail="Insufficient stock",
            )

    await db.commit()

    reloaded = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.variant))
        .where(Order.id == order.id)
    )
    order = reloaded.unique().scalar_one()

    from app.transaction.tasks import cancel_unpaid_order  # noqa: PLC0415
    cancel_unpaid_order.apply_async(args=[order.id], countdown=1800)

    return order


# Allowed order statuses to initiate a refund
_REFUNDABLE_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.PAID,
    OrderStatus.SHIPPED,
    OrderStatus.PARTIALLY_REFUNDED,
})


async def process_partial_refund(
    db: AsyncSession,
    order_id: int,
    request: RefundRequest,
    redis: "Redis",   # type: ignore[name-defined]
) -> dict:
    """
    Process partial refund.
    """
    # 0. Acquire order row lock
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
    )
    order: Order | None = result.scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    # 0.5. Check refund idempotency
    idem_redis_key = _REFUND_IDEM_KEY_TPL.format(key=request.idempotency_key)
    acquired: bool = await redis.set(
        idem_redis_key,
        "1",
        nx=True,
        ex=_REFUND_IDEM_TTL,
    )
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="Refund request already processed",
        )

    try:
        # 1. Validate status
        if order.status not in _REFUNDABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Invalid order status for refund",
            )

        # 2. Validate refund lines and calculate refund amount
        item_ids = [req.order_item_id for req in request.items]
        item_rows_result = await db.execute(
            select(OrderItem)
            .where(
                OrderItem.id.in_(item_ids),
                OrderItem.order_id == order_id,
            )
            .with_for_update()
        )
        order_items: list[OrderItem] = item_rows_result.scalars().all()
        order_item_map: dict[int, OrderItem] = {oi.id: oi for oi in order_items}

        total_refunded_amount = Decimal("0.00")
        refund_detail: list[dict] = []

        for req in request.items:
            order_item = order_item_map.get(req.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=404,
                    detail="OrderItem not found",
                )

            new_refunded_qty = order_item.refunded_quantity + req.refund_qty
            if new_refunded_qty > order_item.quantity:
                raise HTTPException(
                    status_code=422,
                    detail="Refund quantity exceeds purchased quantity",
                )

            # Adjust the last refund to match paid price exactly to eliminate rounding issues
            if new_refunded_qty == order_item.quantity:
                item_refund_amount = (
                    order_item.actual_paid_price - order_item.refunded_amount
                ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)
            else:
                item_refund_amount = (
                    (Decimal(req.refund_qty) / Decimal(order_item.quantity))
                    * order_item.actual_paid_price
                ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)

            order_item.refunded_amount += item_refund_amount
            order_item.refunded_quantity = new_refunded_qty
            order_item.is_fully_refunded = (new_refunded_qty == order_item.quantity)

            # 3. Revert stock
            await db.execute(
                text(
                    "UPDATE product_variant "
                    "SET stock = stock + :qty "
                    "WHERE id = :vid"
                ),
                {"qty": req.refund_qty, "vid": order_item.variant_id},
            )

            total_refunded_amount += item_refund_amount
            refund_detail.append({
                "order_item_id": req.order_item_id,
                "refund_qty": req.refund_qty,
                "refund_amount": str(item_refund_amount),
            })

        # 4. Re-evaluate parent order status
        all_items_result = await db.execute(
            select(OrderItem).where(OrderItem.order_id == order_id)
        )
        all_items: list[OrderItem] = all_items_result.scalars().all()

        if all(oi.is_fully_refunded for oi in all_items):
            order.status = OrderStatus.REFUNDED
        else:
            order.status = OrderStatus.PARTIALLY_REFUNDED

        new_status_val = order.status.value
        await db.commit()
    except Exception as exc:
        try:
            await redis.delete(idem_redis_key)
        except Exception:
            pass
        raise exc

    return {
        "order_id": order_id,
        "new_status": new_status_val,
        "refunded_amount": str(total_refunded_amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)),
        "detail": refund_detail,
    }


# Order Split by Warehouse Service

async def split_order_by_warehouse(
    db: AsyncSession,
    order_id: int,
) -> list[Order]:
    """
    Split parent order by warehouse.
    """
    lock_result = await db.execute(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
    )
    order: Order | None = lock_result.scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != OrderStatus.PAID:
        raise HTTPException(
            status_code=409,
            detail="Order must be in PAID status to split",
        )
    if order.parent_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Child orders cannot be split",
        )

    items_result = await db.execute(
        select(OrderItem)
        .where(OrderItem.order_id == order_id)
        .options(joinedload(OrderItem.variant))
    )
    order_items: list[OrderItem] = items_result.scalars().all()

    if not order_items:
        raise HTTPException(
            status_code=422,
            detail="Order has no items to split",
        )

    from collections import defaultdict  # noqa: PLC0415

    warehouse_groups: dict[str, list[OrderItem]] = defaultdict(list)
    for item in order_items:
        wc = item.variant.warehouse_code if item.variant.warehouse_code else "__UNKNOWN__"
        warehouse_groups[wc].append(item)

    if len(warehouse_groups) == 1:
        return [order]

    child_orders: list[Order] = []

    for warehouse_code, items in warehouse_groups.items():
        sub_total: Decimal = sum(
            item.actual_paid_price for item in items
        ).quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)

        child_order = Order(
            user_id=order.user_id,
            status=OrderStatus.PAID,
            total_amount=sub_total,
            idempotency_key=None,
            parent_id=order.id,
        )
        db.add(child_order)
        await db.flush()

        for item in items:
            item.order_id = child_order.id

        child_orders.append(child_order)

    child_ids = [child.id for child in child_orders]
    order.status = OrderStatus.SPLIT
    await db.commit()

    reloaded_result = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.variant))
        .where(Order.id.in_(child_ids))
    )
    return reloaded_result.scalars().all()


# Order Query Service

async def get_orders(db: AsyncSession, limit: int = 50) -> list[Order]:
    """
    Batch query orders.
    """
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.variant))
        .order_by(Order.id.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def get_order_by_id(db: AsyncSession, order_id: int) -> Order | None:
    """
    Query order by ID.
    """
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.variant))
        .where(Order.id == order_id)
    )
    return result.scalar_one_or_none()


async def seed_test_data(db: AsyncSession) -> dict[str, Any]:
    """
    Seed test data.
    """
    import uuid

    p1 = Product(name="Test Pro Dev Laptop Pro", is_active=True)
    p2 = Product(name="Test Noise-Canceling Headphones Ultra", is_active=True)
    db.add_all([p1, p2])
    await db.flush()

    v1 = ProductVariant(
        product_id=p1.id,
        price=Decimal("33.33"),
        stock=1000,
        warehouse_code="WH-SHANGHAI",
        attributes={"color": "Space Gray"},
    )
    v2 = ProductVariant(
        product_id=p2.id,
        price=Decimal("66.67"),
        stock=1000,
        warehouse_code="WH-BEIJING",
        attributes={"color": "White"},
    )
    db.add_all([v1, v2])
    await db.flush()

    o1 = Order(
        user_id=1001,
        status=OrderStatus.PAID,
        total_amount=Decimal("33.33"),
        idempotency_key=f"seed_o1_{uuid.uuid4()}",
    )
    o2 = Order(
        user_id=1002,
        status=OrderStatus.PAID,
        total_amount=Decimal("100.00"),
        idempotency_key=f"seed_o2_{uuid.uuid4()}",
    )
    o3 = Order(
        user_id=1003,
        status=OrderStatus.PAID,
        total_amount=Decimal("99.99"),
        idempotency_key=f"seed_o3_{uuid.uuid4()}",
    )
    db.add_all([o1, o2, o3])
    await db.flush()
    o1_id, o2_id, o3_id = o1.id, o2.id, o3.id

    item1 = OrderItem(
        order_id=o1_id,
        variant_id=v1.id,
        unit_price=Decimal("33.33"),
        quantity=1,
        prorated_discount=Decimal("0.00"),
        actual_paid_price=Decimal("33.33"),
    )
    item2_1 = OrderItem(
        order_id=o2_id,
        variant_id=v1.id,
        unit_price=Decimal("33.33"),
        quantity=1,
        prorated_discount=Decimal("0.00"),
        actual_paid_price=Decimal("33.33"),
    )
    item2_2 = OrderItem(
        order_id=o2_id,
        variant_id=v2.id,
        unit_price=Decimal("66.67"),
        quantity=1,
        prorated_discount=Decimal("0.00"),
        actual_paid_price=Decimal("66.67"),
    )
    item3 = OrderItem(
        order_id=o3_id,
        variant_id=v1.id,
        unit_price=Decimal("33.33"),
        quantity=3,
        prorated_discount=Decimal("0.00"),
        actual_paid_price=Decimal("99.99"),
    )
    db.add_all([item1, item2_1, item2_2, item3])
    await db.commit()

    return {
        "status": "success",
        "message": "Successfully seeded 2 different warehouse SKUs and 3 complex-amount PAID orders",
        "created_orders": [o1_id, o2_id, o3_id],
    }
