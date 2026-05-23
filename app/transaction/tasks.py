import asyncio

from sqlalchemy import text
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.transaction.models import Order, OrderStatus


@celery_app.task(name="cancel_unpaid_order")
def cancel_unpaid_order(order_id: int) -> None:
    """
    Cancel unpaid order after timeout.
    """
    asyncio.run(_cancel_unpaid_order_async(order_id))


async def _cancel_unpaid_order_async(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
            .with_for_update()
        )
        order: Order | None = result.scalar_one_or_none()

        if order is None or order.status != OrderStatus.PENDING:
            return

        order.status = OrderStatus.CANCELLED

        # Sort items by variant_id to avoid AB-BA deadlocks
        for item in sorted(order.items, key=lambda x: x.variant_id):
            await db.execute(
                text(
                    "UPDATE product_variant "
                    "SET stock = stock + :qty "
                    "WHERE id = :variant_id"
                ),
                {"qty": item.quantity, "variant_id": item.variant_id},
            )

        await db.commit()
