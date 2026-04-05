from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.redis import AsyncRedis as Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.redis import get_redis
from app.transaction import services
from app.transaction.models import Order, OrderItem, OrderStatus
from app.transaction.schemas import OrderCreate, OrderResponse, RefundRequest, RefundResponse, SplitOrderResponse
from app.transaction.services import IllegalOrderStateError

router = APIRouter(prefix="/transaction", tags=["Transaction"])


@router.get("/orders/", response_model=list[OrderResponse], summary="List orders")
async def list_orders(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    return await services.get_orders(db, limit=limit)


@router.get("/orders/{order_id}", response_model=OrderResponse, summary="Get order")
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> Order:
    order = await services.get_order_by_id(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/seed", summary="Seed data")
async def seed_data(
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await services.seed_test_data(db)


@router.post("/orders/", response_model=OrderResponse, status_code=201, summary="Create order")
async def create_order(
    order_in: OrderCreate,
    db: AsyncSession = Depends(get_db),
) -> Order:
    return await services.create_order_transaction(
        db, order_in, order_in.idempotency_key
    )


@router.patch("/orders/{order_id}/status", response_model=OrderResponse, summary="Update order status")
async def update_order_status(
    order_id: int,
    target_status: OrderStatus,
    db: AsyncSession = Depends(get_db),
) -> Order:
    result = await db.execute(select(Order).where(Order.id == order_id))
    order: Order | None = result.scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        services.validate_status_transition(order.status, target_status)
    except IllegalOrderStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    order.status = target_status
    await db.commit()

    reloaded = await db.execute(
        select(Order).options(selectinload(Order.items).selectinload(OrderItem.variant)).where(Order.id == order_id)
    )
    return reloaded.unique().scalar_one()


@router.post(
    "/orders/{order_id}/refund",
    response_model=RefundResponse,
    summary="Process partial refund",
    description="Process partial refund",
)
async def refund_order(
    order_id: int,
    request: RefundRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RefundResponse:
    result = await services.process_partial_refund(db, order_id, request, redis)
    return RefundResponse(
        order_id=result["order_id"],
        new_status=result["new_status"],
        refunded_amount=result["refunded_amount"],
        detail=result["detail"],
    )


@router.post(
    "/orders/{order_id}/split",
    response_model=SplitOrderResponse,
    summary="Split order",
    description="Split order by warehouse",
)
async def split_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> SplitOrderResponse:
    child_orders = await services.split_order_by_warehouse(db, order_id)

    if len(child_orders) == 1 and child_orders[0].id == order_id:
        original = child_orders[0]
        return SplitOrderResponse(
            parent_order_id=order_id,
            parent_status=original.status,
            warehouse_count=1,
            child_orders=[],
        )

    return SplitOrderResponse(
        parent_order_id=order_id,
        parent_status=OrderStatus.SPLIT,
        warehouse_count=len(child_orders),
        child_orders=[OrderResponse.model_validate(c) for c in child_orders],
    )
