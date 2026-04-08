from __future__ import annotations

from decimal import Decimal
from typing import List

from pydantic import BaseModel, ConfigDict


class CartItemResponse(BaseModel):
    variant_id: int
    quantity: int
    added_price: Decimal
    current_price: Decimal
    is_price_changed: bool
    is_valid: bool

    model_config = ConfigDict(from_attributes=False)


class CartCheckoutResponse(BaseModel):
    items: List[CartItemResponse]
    total_current_price: Decimal

    model_config = ConfigDict(from_attributes=False)

