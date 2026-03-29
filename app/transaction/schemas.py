from decimal import Decimal
from typing import List

from pydantic import BaseModel, ConfigDict, Field

from app.transaction.models import OrderStatus


class OrderItemCreate(BaseModel):
    variant_id: int
    quantity: int = Field(..., gt=0, le=99)


class OrderItemResponse(BaseModel):
    id: int
    variant_id: int
    unit_price: Decimal
    quantity: int
    prorated_discount: Decimal
    actual_paid_price: Decimal
    refunded_quantity: int
    refunded_amount: Decimal
    is_fully_refunded: bool
    warehouse_code: str | None = None

    model_config = ConfigDict(from_attributes=True)


class OrderCreate(BaseModel):
    user_id: int
    idempotency_key: str = Field(..., min_length=1, max_length=64)
    items: List[OrderItemCreate] = Field(..., min_length=1)


class OrderResponse(BaseModel):
    id: int
    user_id: int
    status: OrderStatus
    total_amount: Decimal
    parent_id: int | None = None
    items: List[OrderItemResponse]

    model_config = ConfigDict(from_attributes=True)


# Refund Schemas

class RefundItemRequest(BaseModel):
    order_item_id: int
    refund_qty: int = Field(..., gt=0, description="Refund quantity")


class RefundRequest(BaseModel):
    items: List[RefundItemRequest] = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1, max_length=64)


class RefundResponse(BaseModel):
    order_id: int
    new_status: OrderStatus
    refunded_amount: Decimal = Field(..., description="Refunded amount")
    detail: List[dict] = Field(default_factory=list, description="Refund details")


# Split Schemas

class SplitOrderResponse(BaseModel):
    parent_order_id: int
    parent_status: OrderStatus
    warehouse_count: int = Field(..., description="Number of warehouses")
    child_orders: List[OrderResponse] = Field(
        ..., description="List of child orders"
    )
